"""雪花 ID 生成器的契约测试。

重点不在"能生成数字"，而在三个容易出事的地方：
    1. 并发下不重复（锁是否真的覆盖了读-改-写）
    2. 时钟回拨时是报错还是悄悄产出重复 ID
    3. 位布局与 decompose 是否自洽（排障时靠它还原时间）
"""

import threading
from datetime import UTC, datetime

import pytest

from app.core.snowflake import (
    EPOCH_MS,
    SEQUENCE_MASK,
    WORKER_ID_MAX,
    ClockMovedBackwardsError,
    SnowflakeGenerator,
    decompose,
    get_snowflake,
)

BASE_MS = EPOCH_MS + 1_000_000


class FakeClock:
    """可控时钟：默认冻结，便于构造"同一毫秒内多次生成"的场景。"""

    def __init__(self, start: int = BASE_MS) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now

    def jump(self, ms: int) -> None:
        self.now += ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gen(clock: FakeClock) -> SnowflakeGenerator:
    return SnowflakeGenerator(worker_id=7, clock=clock)


# ---------------------------------------------------------------- 基本性质


def test_ids_are_strictly_increasing(gen: SnowflakeGenerator) -> None:
    ids = [gen.next_id() for _ in range(1000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ids_are_unique_under_concurrency() -> None:
    """多线程并发生成，任何重复都说明锁没盖住整个读-改-写。"""
    generator = SnowflakeGenerator(worker_id=1)
    results: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        local = [generator.next_id() for _ in range(5000)]
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 40_000
    assert len(set(results)) == 40_000


def test_id_fits_in_signed_63_bits(gen: SnowflakeGenerator) -> None:
    """最高位必须为 0，否则写进 BIGINT（有符号）会变成负数。"""
    for _ in range(100):
        assert gen.next_id() < 2**63


# ---------------------------------------------------------------- 位布局


def test_bit_layout_is_decodable(clock: FakeClock) -> None:
    generator = SnowflakeGenerator(worker_id=1023, clock=clock)
    snowflake_id = generator.next_id()

    parts = decompose(snowflake_id)
    assert parts.worker_id == 1023
    assert parts.timestamp_ms == BASE_MS
    assert parts.sequence == 0


def test_sequence_increments_within_same_millisecond(gen: SnowflakeGenerator) -> None:
    """时钟冻结时，同一毫秒内靠序列号区分。"""
    ids = [gen.next_id() for _ in range(100)]
    sequences = [decompose(i).sequence for i in ids]

    assert sequences == list(range(100))
    assert all(decompose(i).timestamp_ms == BASE_MS for i in ids)


def test_sequence_overflow_moves_to_next_millisecond(
    gen: SnowflakeGenerator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4096 个序列号用完后必须等到下一毫秒，而不是回绕产生重复 ID。"""
    waits: list[int] = []

    def fake_wait(last_ms: int) -> int:
        waits.append(last_ms)
        return last_ms + 1

    monkeypatch.setattr(gen, "_wait_next_ms", fake_wait)

    for _ in range(SEQUENCE_MASK + 1):
        gen.next_id()
    assert waits == []

    overflow_id = gen.next_id()
    assert len(waits) == 1
    parts = decompose(overflow_id)
    assert parts.timestamp_ms == BASE_MS + 1
    assert parts.sequence == 0


def test_decompose_restores_utc_time(gen: SnowflakeGenerator) -> None:
    parts = decompose(gen.next_id())
    assert parts.created_at.tzinfo is UTC
    assert parts.created_at == datetime.fromtimestamp(BASE_MS / 1000, tz=UTC)


# ---------------------------------------------------------------- 时钟回拨


def test_large_clock_rollback_raises(gen: SnowflakeGenerator, clock: FakeClock) -> None:
    """回拨 100ms 必须报错：继续生成会产出重复 ID。"""
    gen.next_id()
    clock.jump(-100)

    with pytest.raises(ClockMovedBackwardsError):
        gen.next_id()


def test_small_clock_drift_waits_instead_of_raising(
    gen: SnowflakeGenerator, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """小幅回拨（NTP 校时）应等待追上，不应让业务失败。"""
    first = decompose(gen.next_id())
    clock.jump(-2)
    monkeypatch.setattr(gen, "_wait_next_ms", lambda last_ms: last_ms + 1)

    parts = decompose(gen.next_id())
    # 等到原毫秒后继续，序列号接着上一次往后排
    assert parts.timestamp_ms == first.timestamp_ms
    assert parts.sequence == first.sequence + 1


# ---------------------------------------------------------------- 参数校验


@pytest.mark.parametrize("worker_id", [-1, WORKER_ID_MAX + 1, 2000])
def test_invalid_worker_id_rejected(worker_id: int) -> None:
    with pytest.raises(ValueError, match="worker_id"):
        SnowflakeGenerator(worker_id=worker_id)


def test_negative_id_rejected_in_decompose() -> None:
    with pytest.raises(ValueError, match="不能为负"):
        decompose(-1)


def test_singleton_is_reused() -> None:
    """每次请求新建生成器会丢掉 last_ms/sequence，同毫秒内就可能重复。"""
    get_snowflake.cache_clear()
    try:
        assert get_snowflake() is get_snowflake()
    finally:
        get_snowflake.cache_clear()
