"""请求级上下文：目前只有 trace_id。

为什么用 ContextVar 而不是把 trace_id 一路当参数传：
    一次请求会穿过中间件 → 路由 → service → repository，
    逐层加参数会污染所有函数签名，而 99% 的层根本不关心它。
    ContextVar 是 asyncio 里传递请求级数据的标准做法，
    每个请求（每个 task）各有一份，不会串。

为什么不放 app.state：
    app.state 是进程级的，所有并发请求共享同一个值，必然串号。
"""

from contextvars import ContextVar, Token

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")


def set_trace_id(value: str) -> Token[str]:
    return _trace_id.set(value)


def reset_trace_id(token: Token[str]) -> None:
    _trace_id.reset(token)


def current_trace_id() -> str:
    """当前请求的 trace_id；不在请求上下文里时返回空串。"""
    return _trace_id.get()
