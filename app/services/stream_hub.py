"""SSE 断线续传：把「生成」从「连接」里拆出来。

为什么必须拆
------------
原来生成跑在请求处理函数里：客户端一断，`asyncio.CancelledError` 把生成一起
掐了，这一轮就白跑了 —— 用户手机切个后台、地铁过个隧道，回来看到半句话，
而且再也问不出下半句。要"续传"，生成就必须活过连接：

    生产者（asyncio.Task）  跑模型 → 每块 XADD 进 Redis Stream → 写库收尾
    消费者（请求的生成器）    XREAD 从 Last-Event-ID 之后读 → 编码成 SSE 帧

连接只是"读指针"，断了再连一个就是了；生成不受影响。

为什么用 Redis Stream，而不是内存队列
------------------------------------
1. **进程无关**：重连可能落在另一个 worker 上，内存队列读不到；
2. **自带有序 ID**：`XADD` 返回的 `1761666000000-3` 直接当 SSE 的 `id:`，
   客户端原样回传、服务端 `XREAD` 从它之后读 —— 不用自己维护 offset，
   也不用防客户端编造位置（ID 是服务端分配的，跳着传没有意义）；
3. **不是消费队列**：Stream 读完不删除，两个标签页能同时读，
   同一个位置也能重读两遍（这正是"刷新页面继续看"需要的）。

四个 key
--------
    sse:turn:{id}    数据流，XADD 追加，带 TTL 兜底
    sse:alive:{id}   消费者心跳：客户端全断超过 grace 就停生成（省 token）
    sse:beat:{id}    生产者心跳：消费端据此判断"生产者死了，不会再有下文"
    sse:end:{id}     结束标记：生产者写完终止帧后设置

为什么要有 alive 和 beat 两个心跳
--------------------------------
它们方向相反，缺一个就有一种情况判断不了：
    - 只有 beat：客户端走了但生成还在跑，token 白烧到最后一块；
    - 只有 alive：生产者进程崩了，消费者会一直等一个永远不来的块
      （XREAD 每次超时就重来，连接挂着、用户对着转圈）。

代价与取舍
----------
断线不再立刻停止生成，**这是这个功能的本质代价**：不知道客户端会不会回来，
就只能等 grace 窗口。所以 grace 不宜设大（默认 30s），而且生成结束后缓冲只留
`SSE_RESUME_TTL_SECONDS`（默认 10 分钟），过期后续传返回 410。
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sse_starlette import JSONServerSentEvent, ServerSentEvent

from app.core.config import Settings
from app.core.sse import (
    EVENT_DELTA,
    EVENT_DONE,
    EVENT_ERROR,
    EVENT_META,
    EVENT_START,
    SSE_DONE,
)
from app.db.enums import MessageStatus
from app.schemas.chat import (
    ChatMessage,
    ChatRequest,
    StreamError,
    StreamMeta,
    StreamStart,
)
from app.services.conversation_service import ConversationService, TurnContext
from app.services.exceptions import LLMUpstreamError, ServiceError
from app.services.llm_service import LLMService, StreamFinished, TokenDelta

logger = logging.getLogger(__name__)

# 单轮最多保留多少个分片。按块计数而不是按字节：一块通常就是一个 token 或几个字。
# 超了从头截断（XADD MAXLEN），意味着"从头重放"可能拿不到最开头的几块 ——
# 但 10000 块远超任何正常回答，这里是防跑飞的护栏，不是容量规划。
_ENTRY_MAXLEN = 10_000

# 每次 XREAD 最多取多少块。取大了会把大量帧一次性推给客户端（首屏井喷），
# 取小了空转次数变多。200 大约对应一两秒的输出量。
_READ_COUNT = 200

# 单次 XREAD 阻塞多久（毫秒）。必须 < REDIS_SOCKET_TIMEOUT_SECONDS，
# 否则是 socket 先超时、而不是 Redis 先返回"没有新数据"。
_READ_BLOCK_MS = 1_000

# 生产者心跳 TTL / 刷新间隔。TTL 也是消费端判定"生产者已死"的等待上限。
_BEAT_TTL_SECONDS = 30
_BEAT_INTERVAL_SECONDS = 5.0

# 检查"还有没有人在听"的间隔。每块都查就是每个 token 一次 Redis GET，
# 高频流下这比 token 本身还贵，所以按墙钟节流。
_ALIVE_CHECK_INTERVAL_SECONDS = 2.0

# 消费端刷新自己心跳的间隔，要比 grace 小得多，否则正常的读延迟会被误判成断线
_ALIVE_TOUCH_INTERVAL_SECONDS = 5.0

# 从头开始的 Stream ID
STREAM_START = "0-0"


@dataclass(frozen=True)
class Frame:
    """Stream 里的一条记录，同时也是要发出去的那一帧。"""

    event: str
    data: str
    entry_id: str


class StreamBuffer:
    """Redis Stream 上的分片缓冲。只管存取，不含业务判断。"""

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    # ---- key ----

    @staticmethod
    def _stream(turn_id: int) -> str:
        return f"sse:turn:{turn_id}"

    @staticmethod
    def _alive(turn_id: int) -> str:
        return f"sse:alive:{turn_id}"

    @staticmethod
    def _beat(turn_id: int) -> str:
        return f"sse:beat:{turn_id}"

    @staticmethod
    def _end(turn_id: int) -> str:
        return f"sse:end:{turn_id}"

    # ---- 读写 ----

    async def append(self, turn_id: int, event: str, data: str) -> str:
        """追加一帧，返回 Stream entry id（就是这一帧的 SSE `id:`）。"""
        return await self._redis.xadd(
            self._stream(turn_id),
            {"event": event, "data": data},
            maxlen=_ENTRY_MAXLEN,
            approximate=True,
        )

    async def read(
        self, turn_id: int, after: str, *, block_ms: int | None = _READ_BLOCK_MS
    ) -> list[Frame]:
        """读 `after` 之后的帧。`block_ms=None` 为非阻塞探测。"""
        response = await self._redis.xread(
            {self._stream(turn_id): after}, count=_READ_COUNT, block=block_ms
        )
        if not response:
            return []
        _, entries = response[0]
        return [
            Frame(event=fields["event"], data=fields["data"], entry_id=entry_id)
            for entry_id, fields in entries
        ]

    async def exists(self, turn_id: int) -> bool:
        return bool(await self._redis.exists(self._stream(turn_id)))

    async def expire(self, turn_id: int) -> None:
        """给数据流设 TTL。生产者跑着的时候每次心跳顺手续一次。"""
        await self._redis.expire(self._stream(turn_id), self._ttl)

    # ---- 心跳与结束标记 ----

    async def start(self, turn_id: int, grace_seconds: int) -> None:
        """生产者启动前的占位：先把心跳和"有人在听"立起来。

        必须先于消费者读第一帧完成，否则消费者会看到"没有 beat"，
        误判成生产者已死。
        """
        pipe = self._redis.pipeline()
        pipe.set(self._beat(turn_id), "1", ex=_BEAT_TTL_SECONDS)
        pipe.set(self._alive(turn_id), "1", ex=grace_seconds)
        await pipe.execute()

    async def beat(self, turn_id: int) -> None:
        """生产者心跳 + 顺手续一下数据流的 TTL。

        续 TTL 放这里而不是每块都续：每块一次 EXPIRE 是白给的一条命令，
        而 5 秒一次的精度对"缓冲区多久过期"完全够用。
        """
        pipe = self._redis.pipeline()
        pipe.set(self._beat(turn_id), "1", ex=_BEAT_TTL_SECONDS)
        pipe.expire(self._stream(turn_id), self._ttl)
        await pipe.execute()

    async def producer_alive(self, turn_id: int) -> bool:
        return bool(await self._redis.exists(self._beat(turn_id)))

    async def touch_consumer(self, turn_id: int, grace_seconds: int) -> None:
        """续客户端心跳。超过 grace 不续，生成端就会收到"没人听了"。"""
        await self._redis.set(self._alive(turn_id), "1", ex=grace_seconds)

    async def any_consumer_alive(self, turn_id: int) -> bool:
        return bool(await self._redis.exists(self._alive(turn_id)))

    async def is_finished(self, turn_id: int) -> bool:
        return bool(await self._redis.exists(self._end(turn_id)))

    async def finish(self, turn_id: int) -> None:
        """打结束标记并给所有 key 设 TTL。

        结束标记不能省：消费者要靠它区分"暂时没有新数据"和"不会再有新数据"。
        没有它，客户端连上一条已结束的流会一直等到 Redis 连接超时。
        """
        pipe = self._redis.pipeline()
        pipe.set(self._end(turn_id), "1", ex=self._ttl)
        pipe.expire(self._stream(turn_id), self._ttl)
        pipe.delete(self._beat(turn_id))
        pipe.delete(self._alive(turn_id))
        await pipe.execute()


# --------------------------------------------------------------- 生产者


class TurnRunner:
    """一轮生成的生产者：写库、写缓冲、谁都不依赖连接。"""

    def __init__(
        self,
        *,
        service: LLMService,
        conversations: ConversationService,
        context: TurnContext,
        payload: ChatRequest,
        history: Sequence[ChatMessage],
        buffer: StreamBuffer,
        settings: Settings,
    ) -> None:
        self._service = service
        self._conversations = conversations
        self._context = context
        self._payload = payload
        self._history = history
        self._buffer = buffer
        self._grace = settings.SSE_RESUME_GRACE_SECONDS
        # 预填 title 之类的上下文信息只在 meta 里用，这里保留整个 context

        self._turn_id: int = context.assistant_message_id or 0
        self._parts: list[str] = []
        self._last_beat = 0.0
        self._last_alive_check = 0.0
        self._ttl_set = False

    async def run(self) -> None:
        """跑完一轮。**不向外抛异常** —— 这是独立任务，抛出去没人接。"""
        try:
            finished = await self._stream()
            await self._complete(finished)
        except _Abandoned:
            # `_tick` 已经写好了 INTERRUPTED 和终止帧，这里只需要安静退出
            logger.info("生成已中止 turn=%s", self._turn_id)
        except asyncio.CancelledError:
            # 停机 / 主动取消：保住已生成的部分，让用户回来能看到"说到哪儿了"
            logger.info("生成被取消 turn=%s，已生成 %d 块", self._turn_id, len(self._parts))
            await self._shielded(self._interrupt())
            raise
        except (LLMUpstreamError, ServiceError) as exc:
            # 流已开始，状态码改不了了，只能用事件传递
            logger.error(
                "对话流中断 code=%s turn=%s message=%s",
                exc.error_code.code,
                self._turn_id,
                exc.message,
                exc_info=exc,
            )
            await self._fail(exc.error_code.code, exc.message)
        except Exception:  # noqa: BLE001
            # 兜底：未知异常也要给客户端一个终止帧，否则它会一直等
            logger.exception("对话流异常 turn=%s", self._turn_id)
            await self._fail(10500, "服务内部异常")
        finally:
            await self._shielded(self._buffer.finish(self._turn_id))

    async def _stream(self) -> StreamFinished | None:
        """把模型输出搬进缓冲，返回结束元信息（网关不发时为 None）。"""
        # 幂等：路由为了保证消费者不会抢在生产者之前读，已经先立过一次，
        # 这里再立一次是为了让 TurnRunner 单独使用时也自洽
        await self._buffer.start(self._turn_id, self._grace)
        self._last_beat = time.monotonic()

        async for item in self._service.astream_chat(self._payload, self._history):
            await self._tick()
            if isinstance(item, TokenDelta):
                self._parts.append(item.text)
                await self._append(
                    EVENT_DELTA, json.dumps({"delta": item.text}, ensure_ascii=False)
                )
            else:
                # 服务层保证 StreamFinished 是最后一个事件，拿到就可以收尾了
                return item
        return None

    async def _tick(self) -> None:
        """心跳与"还有没有人在听"的节流检查。"""
        now = time.monotonic()
        if now - self._last_beat >= _BEAT_INTERVAL_SECONDS:
            self._last_beat = now
            await self._buffer.beat(self._turn_id)

        if now - self._last_alive_check < _ALIVE_CHECK_INTERVAL_SECONDS:
            return
        self._last_alive_check = now
        if await self._buffer.any_consumer_alive(self._turn_id):
            return

        # 没人听了：停掉。继续跑是纯烧钱 —— 生成的每个字都没人看，
        # 而且这个用户下次进来时也不会要这半截回答（库里留 INTERRUPTED）。
        logger.info("无人接收，提前停止生成 turn=%s 已生成 %d 块", self._turn_id, len(self._parts))
        await self._interrupt()
        await self._append(
            EVENT_ERROR,
            StreamError(code=22408, message="长时间没有客户端接收，生成已中止").model_dump_json(),
        )
        raise _Abandoned()

    async def _append(self, event: str, data: str) -> None:
        await self._buffer.append(self._turn_id, event, data)
        if not self._ttl_set:
            # 第一块落地后数据流才真的存在，这时候设 TTL 才有效
            # （对不存在的 key 执行 EXPIRE 是空操作，会留下一个永不过期的 key）
            await self._buffer.expire(self._turn_id)
            self._ttl_set = True

    async def _complete(self, finished: StreamFinished | None) -> None:
        """正常收尾：**先写库再发 done**。"""
        answer = "".join(self._parts)
        if finished is not None:
            await self._conversations.complete_turn(
                self._context,
                answer=answer,
                prompt_tokens=finished.prompt_tokens,
                completion_tokens=finished.completion_tokens,
            )
            meta = StreamMeta(
                model=finished.model,
                prompt_tokens=finished.prompt_tokens,
                completion_tokens=finished.completion_tokens,
                conversation_id=str(self._context.conversation_id),
                message_id=str(self._turn_id),
            )
        else:
            # 网关只发增量就断流（没给 on_chat_model_end）：内容照存，
            # 只是没有用量可报
            logger.warning("流结束但没有用量元信息 turn=%s", self._turn_id)
            await self._conversations.complete_turn(self._context, answer=answer)
            meta = StreamMeta(
                model=self._service.model,
                conversation_id=str(self._context.conversation_id),
                message_id=str(self._turn_id),
            )

        await self._append(EVENT_META, meta.model_dump_json())
        await self._append(EVENT_DONE, SSE_DONE)

    async def _interrupt(self) -> None:
        """把已生成的部分存成 INTERRUPTED，不再改动其余状态。"""
        await self._conversations.complete_turn(
            self._context, answer="".join(self._parts), status=MessageStatus.INTERRUPTED
        )

    async def _fail(self, code: int, message: str) -> None:
        await self._conversations.fail_turn(
            self._context, reason=message, answer="".join(self._parts)
        )
        await self._append(EVENT_ERROR, StreamError(code=code, message=message).model_dump_json())

    async def _shielded(self, action) -> None:
        """在不被取消打断的前提下跑完收尾写库。

        任务已进入取消态时直接 `await` 会被立刻打断，那条消息就永远停在
        STREAMING。`ensure_future` + `shield` 让收尾脱离当前任务的取消域跑完。
        失败只记日志：收尾出问题不该盖住用户真正关心的错误。
        """
        task = asyncio.ensure_future(action)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("对话收尾失败 turn=%s", self._turn_id)


class _Abandoned(Exception):
    """内部信号：无人接收，`_stream` 主动退出。"""


# --------------------------------------------------------------- 消费者


async def stream_frames(
    *,
    buffer: StreamBuffer,
    context: TurnContext,
    after: str,
    settings: Settings,
) -> AsyncIterator[ServerSentEvent]:
    """把缓冲里的帧搬给客户端，从 `after` 之后开始。

    `after` 就是客户端上次收到的最后一个 `id:`；首次连接传 `"0-0"`。
    """
    turn_id = context.assistant_message_id or 0

    yield JSONServerSentEvent(
        data=StreamStart(
            conversation_id=str(context.conversation_id), message_id=str(turn_id)
        ).model_dump(),
        event=EVENT_START,
    )

    last = after
    last_touch = 0.0
    # 续命间隔必须比 grace 小得多，否则"正常读着"也会被判成断线。
    # 取 grace/3：既有两次容错，又不至于每个 XREAD 轮次都写一次 Redis。
    touch_interval = max(
        0.5, min(_ALIVE_TOUCH_INTERVAL_SECONDS, settings.SSE_RESUME_GRACE_SECONDS / 3)
    )
    while True:
        now = time.monotonic()
        if now - last_touch >= touch_interval:
            last_touch = now
            await buffer.touch_consumer(turn_id, settings.SSE_RESUME_GRACE_SECONDS)

        frames = await buffer.read(turn_id, last)
        if not frames:
            # 没有新数据：要么生成还没吐下一块，要么已经结束了
            if await buffer.is_finished(turn_id):
                # 收尾和读取之间有竞态窗口，结束时再做一次非阻塞读，防止漏帧
                frames = await buffer.read(turn_id, last, block_ms=None)
                if not frames:
                    # 客户端可能问的是"结束之后的位置"（上次已经收完 done），
                    # 这时补一个 done 让它状态机收尾，而不是干等
                    yield ServerSentEvent(data=SSE_DONE, event=EVENT_DONE)
                    return
            elif not await buffer.producer_alive(turn_id):
                # 生产者消失且没打结束标记 = 进程死了，不会再有下文。
                # 不这么判的话，这里会每秒钟重试一次 XREAD，永远等下去。
                yield JSONServerSentEvent(
                    data=StreamError(
                        code=22408, message="生成已中断，已生成的内容可以重新读取"
                    ).model_dump(),
                    event=EVENT_ERROR,
                )
                return
            else:
                continue

        for frame in frames:
            yield ServerSentEvent(data=frame.data, event=frame.event, id=frame.entry_id)
            last = frame.entry_id
            if frame.event in (EVENT_DONE, EVENT_ERROR):
                return


@dataclass
class TurnRegistry:
    """在跑的生产者任务。

    必须持有引用：`asyncio` 只保留任务的弱引用，创建后没人引用会被 GC 掉，
    表现是"回答经常说到一半就停了"，而且没有异常、没有日志 —— 极难排查。
    """

    tasks: dict[int, asyncio.Task] = field(default_factory=dict)

    def add(self, turn_id: int, task: asyncio.Task) -> None:
        self.tasks[turn_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(turn_id, None))

    async def shutdown(self) -> None:
        """停机：先取消再等收尾，之后才释放 DB / Redis 连接池。

        顺序不能反：收尾要写库，连接池先关掉的话 CancelledError 分支里的
        写库会再抛一层异常，那条消息就永远停在 STREAMING。
        """
        pending = list(self.tasks.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
