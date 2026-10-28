"""雪花 ID 生成器：64 位、时间有序、多实例不冲突。

位布局（最高位恒为 0，保证落在有符号 BIGINT 的正数区间）：

    0 | 41 bit 毫秒时间戳 | 10 bit 机器号 | 12 bit 序列号
      |<---- 69 年 ---->|  |<-- 1024 -->|  |<-- 4096/ms -->|

为什么不用别的方案：
    自增主键    多实例部署必然冲突，而且 ID 直接暴露业务量（竞对能算出日订单数）
    UUIDv4      无序。作为 InnoDB 聚簇索引主键会导致随机插入、频繁页分裂，
                写入吞吐随数据量增长而下降，且 36 字符比 8 字节多占索引空间
    雪花        全局唯一 + 单调递增，主键即时间序，插入集中在索引尾部

对外暴露时必须转成字符串：ID 远超 2^53，JavaScript 的 Number 会静默丢精度。
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

logger = logging.getLogger(__name__)

# 起始纪元 2024-01-01T00:00:00Z（毫秒）。
#
# 这个值一旦上线就不可更改：改纪元会让时间戳字段整体平移，
# 新旧 ID 可能落在同一区间从而撞车。所以它写死在代码里，
# 不做成环境变量 —— 能配置的东西就会被配错。
EPOCH_MS = 1_704_067_200_000

WORKER_ID_BITS = 10
SEQUENCE_BITS = 12

WORKER_ID_MAX = (1 << WORKER_ID_BITS) - 1  # 1023
SEQUENCE_MASK = (1 << SEQUENCE_BITS) - 1  # 4095

WORKER_ID_SHIFT = SEQUENCE_BITS  # 12
TIMESTAMP_SHIFT = SEQUENCE_BITS + WORKER_ID_BITS  # 22

# 允许的时钟回拨容忍度（毫秒）。
# NTP 小幅校时很常见，直接抛异常会让业务无故失败；
# 但真正的回拨（比如运维手动改系统时间）必须拦下来 ——
# 继续生成会产出重复 ID，这比报错严重得多。
MAX_CLOCK_DRIFT_MS = 5


class ClockMovedBackwardsError(RuntimeError):
    """系统时钟回拨超过容忍度。宁可报错，也不生成可能重复的 ID。"""


@dataclass(frozen=True)
class SnowflakeParts:
    """把一个雪花 ID 拆开看，排障时用得上。"""

    timestamp_ms: int
    worker_id: int
    sequence: int

    @property
    def created_at(self) -> datetime:
        """ID 的生成时刻（UTC）。不用查库就能知道记录是什么时候产生的。"""
        return datetime.fromtimestamp(self.timestamp_ms / 1000, tz=UTC)


class SnowflakeGenerator:
    """线程安全的雪花 ID 生成器。

    实例应当进程内复用（见 get_snowflake），每次请求新建会丢掉
    last_ms / sequence 状态，同一毫秒内就可能生成重复 ID。
    """

    def __init__(
        self,
        worker_id: int,
        *,
        epoch_ms: int = EPOCH_MS,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not 0 <= worker_id <= WORKER_ID_MAX:
            raise ValueError(f"worker_id 必须在 0..{WORKER_ID_MAX} 之间，收到：{worker_id}")
        self._worker_id = worker_id
        self._epoch_ms = epoch_ms
        # 注入时钟是为了让测试可控，生产用默认的毫秒时间戳
        self._clock = clock or (lambda: time.time_ns() // 1_000_000)
        self._lock = threading.Lock()
        self._last_ms = -1
        self._sequence = 0

    @property
    def worker_id(self) -> int:
        return self._worker_id

    def next_id(self) -> int:
        """生成下一个 ID。

        锁内没有 await，只做几次整数运算，持锁时间是微秒级 ——
        在 asyncio 里直接调用不会阻塞事件循环，不必改成 async。
        """
        with self._lock:
            now = self._clock()

            if now < self._last_ms:
                now = self._handle_clock_backwards(now)

            if now == self._last_ms:
                self._sequence = (self._sequence + 1) & SEQUENCE_MASK
                if self._sequence == 0:
                    # 本毫秒的 4096 个序列号用完了，自旋等到下一毫秒。
                    # 单机 4096/ms 远超实际写入量，走到这里说明流量异常。
                    now = self._wait_next_ms(self._last_ms)
            else:
                self._sequence = 0

            self._last_ms = now
            return (
                ((now - self._epoch_ms) << TIMESTAMP_SHIFT)
                | (self._worker_id << WORKER_ID_SHIFT)
                | self._sequence
            )

    def _handle_clock_backwards(self, now: int) -> int:
        drift = self._last_ms - now
        if drift > MAX_CLOCK_DRIFT_MS:
            raise ClockMovedBackwardsError(
                f"系统时钟回拨 {drift}ms，超过容忍度 {MAX_CLOCK_DRIFT_MS}ms；"
                "继续生成会产出重复 ID，请先校准时间"
            )
        # 小幅回拨：等时钟追上上次的毫秒再继续，代价是几毫秒延迟
        logger.warning("检测到时钟小幅回拨 %dms，等待追上", drift)
        return self._wait_next_ms(self._last_ms - 1)

    def _wait_next_ms(self, last_ms: int) -> int:
        now = self._clock()
        while now <= last_ms:
            time.sleep(0.0005)
            now = self._clock()
        return now


def decompose(snowflake_id: int, *, epoch_ms: int = EPOCH_MS) -> SnowflakeParts:
    """把雪花 ID 拆回 时间戳 / 机器号 / 序列号。"""
    if snowflake_id < 0:
        raise ValueError(f"雪花 ID 不能为负，收到：{snowflake_id}")
    return SnowflakeParts(
        timestamp_ms=(snowflake_id >> TIMESTAMP_SHIFT) + epoch_ms,
        worker_id=(snowflake_id >> WORKER_ID_SHIFT) & WORKER_ID_MAX,
        sequence=snowflake_id & SEQUENCE_MASK,
    )


@lru_cache(maxsize=1)
def get_snowflake() -> SnowflakeGenerator:
    """进程级单例。

    多实例部署时每个实例必须配置不同的 SNOWFLAKE_WORKER_ID，
    否则不同机器会生成完全相同的 ID。
    """
    from app.core.config import get_settings

    settings = get_settings()
    generator = SnowflakeGenerator(settings.SNOWFLAKE_WORKER_ID)
    logger.info("雪花 ID 生成器就绪 worker_id=%d epoch_ms=%d", generator.worker_id, EPOCH_MS)
    return generator
