"""SSE 协议约定：帧编码交给 sse-starlette，这里只留应用自己的约定。

为什么用库而不是自己写：
    帧编码（多行 data 拆分）、ping 心跳、客户端断开检测、优雅停机
    都是协议层的通用问题，sse-starlette 已经处理过并跟着 Starlette 演进。
    自己维护这 90 行，等于给自己加一份长期维护成本。

库负责的：EventSourceResponse 的 ping、断线取消、shutdown_event 收尾
这里负责的：业务侧的结束哨兵、以及流内事件的命名约定

关于 `id:`（断线续传的锚点）：
    每一帧的 id 就是 Redis Stream 的 entry id（形如 `1761666000000-3`），
    客户端原样存下来，重连时作为 `Last-Event-ID` 回传，服务端从它之后接着读。
    用 Stream 自带的 id 而不是自己发一个自增序号：那个 id 是服务端分配、
    全局单调的，客户端不可能自己编造出一个"跳着读"的位置。
"""

SSE_DONE = "[DONE]"

# 流内事件名：客户端按 event 分流处理
EVENT_START = "start"  # 首帧：给出 conversation_id / message_id（续传和评价都要）
EVENT_DELTA = "delta"
EVENT_META = "meta"
EVENT_DONE = "done"
EVENT_ERROR = "error"
