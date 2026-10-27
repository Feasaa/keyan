"""SSE 编码与心跳测试。

这层是纯协议逻辑，不依赖 app、不依赖网络 —— 拆出来的价值就在这里：
可以只测编码函数，不必起服务、不必假装一个 LLM。
"""

import asyncio

from app.core.sse import HEARTBEAT, format_event, with_heartbeat


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 编码


def test_single_line_data() -> None:
    assert format_event("hello") == "data: hello\n\n"


def test_dict_is_serialized_as_json() -> None:
    assert format_event({"delta": "你好"}) == 'data: {"delta": "你好"}\n\n'


def test_multiline_text_splits_into_multiple_data_lines() -> None:
    """多行必须逐行加 data: 前缀，否则客户端只收到第一行。"""
    assert format_event("第一行\n第二行") == "data: 第一行\ndata: 第二行\n\n"


def test_event_and_id_precede_data() -> None:
    frame = format_event("[DONE]", event="done", event_id="7")

    assert frame == "id: 7\nevent: done\ndata: [DONE]\n\n"


# ------------------------------------------------------------------ 心跳


async def _collect(source, interval: float) -> list[str]:
    return [chunk async for chunk in with_heartbeat(source, interval)]


def test_passes_through_chunks_in_order() -> None:
    async def source():
        for item in ["a", "b", "c"]:
            yield item

    assert run(_collect(source(), 1.0)) == ["a", "b", "c"]


def test_emits_heartbeat_while_idle() -> None:
    async def slow_source():
        await asyncio.sleep(0.2)
        yield "late"

    chunks = run(_collect(slow_source(), 0.05))

    assert HEARTBEAT in chunks
    assert chunks[-1] == "late"


def test_propagates_source_exception() -> None:
    async def failing_source():
        yield "first"
        raise RuntimeError("upstream boom")

    try:
        run(_collect(failing_source(), 1.0))
    except RuntimeError as exc:
        assert str(exc) == "upstream boom"
    else:  # pragma: no cover
        raise AssertionError("异常应当透传给调用方")
