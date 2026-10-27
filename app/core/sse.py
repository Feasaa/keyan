"""SSE（Server-Sent Events）编码与心跳工具。

对外只暴露三个东西：
    format_event  把任意数据编码成一帧 SSE（多行文本按规范拆成多条 data:）
    with_heartbeat 给异步流插入心跳注释，防止代理/网关因空闲断开连接
    SSE_DONE      流结束哨兵，客户端据此停止读取
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

SSE_DONE = "[DONE]"

# 以冒号开头的行是 SSE 注释，客户端会忽略，仅用于保活
HEARTBEAT = ": keep-alive\n\n"


def format_event(
    data: Any,
    *,
    event: str | None = None,
    event_id: str | None = None,
) -> str:
    """把数据编码成一帧 SSE 报文（以空行结尾）。

    str 原样输出，其余类型按 JSON 序列化（ensure_ascii=False 保证中文可读）。
    多行内容必须逐行加 `data:` 前缀，否则只有第一行会被客户端接收。
    """
    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    if event is not None:
        lines.append(f"event: {event}")

    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    for line in payload.splitlines() or [""]:
        lines.append(f"data: {line}")

    return "\n".join(lines) + "\n\n"


async def _pump(
    source: AsyncIterator[str],
    queue: "asyncio.Queue[tuple[str, Any]]",
) -> None:
    """把上游流推入队列：生产者与消费者解耦，便于在空闲时插心跳。"""
    try:
        async for item in source:
            await queue.put(("chunk", item))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await queue.put(("error", exc))
    else:
        await queue.put(("end", None))


async def with_heartbeat(
    source: AsyncIterator[str],
    interval: float,
) -> AsyncIterator[str]:
    """在 source 之上叠加心跳。

    实现要点：心跳不能直接对上游 __anext__ 做超时取消 —— 取消一个进行中的
    异步生成器会把它置于不可恢复状态。所以用独立生产者任务 + 有界队列，
    消费者对 queue.get() 超时（取消队列读取是安全的）。
    队列有界（maxsize=64）形成背压：客户端读得慢时生产者会等待，不会堆积内存。
    """
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=64)
    producer = asyncio.create_task(_pump(source, queue))

    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield HEARTBEAT
                continue

            if kind == "end":
                return
            if kind == "error":
                raise value
            yield value
    finally:
        producer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await producer
