"""SSE 协议约定：帧编码交给 sse-starlette，这里只留应用自己的约定。

为什么用库而不是自己写：
    帧编码（多行 data 拆分）、ping 心跳、客户端断开检测、优雅停机
    都是协议层的通用问题，sse-starlette 已经处理过并跟着 Starlette 演进。
    自己维护这 90 行，等于给自己加一份长期维护成本。

库负责的：EventSourceResponse 的 ping、断线取消、shutdown_event 收尾
这里负责的：业务侧的结束哨兵、以及流内事件的命名约定
"""

SSE_DONE = "[DONE]"

# 流内事件名：客户端按 event 分流处理
EVENT_DELTA = "delta"
EVENT_META = "meta"
EVENT_DONE = "done"
EVENT_ERROR = "error"
