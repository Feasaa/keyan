"""对话端点：一次性响应、SSE 流式响应、配置状态查询。

分层：路由只做参数校验、协议适配与响应组装，链路细节全在 LLMService。

错误处理现在**全部交给全局处理器**（app/core/exception_handlers.py）：
路由里不再出现 HTTPException，业务异常直接往上抛，
由 AppError 自带的错误码决定状态码与文案。

SSE 仍有两种错误通道，这一点没变：
    流开始前 → 抛 AppError，走全局处理器，客户端拿到真实状态码
    流开始后 → 响应头已发出，只能往流里写 event=error 帧
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from sse_starlette import EventSourceResponse, JSONServerSentEvent, ServerSentEvent

from app.core.config import Settings, get_settings
from app.core.response import ApiResponse, ok
from app.core.sse import EVENT_DELTA, EVENT_DONE, EVENT_ERROR, EVENT_META, SSE_DONE
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ChatStatusResponse,
    StreamChunk,
    StreamError,
    StreamMeta,
)
from app.services.exceptions import LLMUpstreamError, ServiceError
from app.services.llm_service import (
    LLMService,
    StreamFinished,
    TokenDelta,
    get_llm_service,
)

router = APIRouter()


@router.post(
    "",
    response_model=ApiResponse[ChatResponse],
    summary="对话（一次性返回）",
    description="等待模型生成完毕后一次性返回，适合短回答与服务端调用。",
)
async def chat(
    payload: ChatRequest,
    service: LLMService = Depends(get_llm_service),
) -> ApiResponse[ChatResponse]:
    answer, model_name = service.chat(payload)
    return ok(ChatResponse(answer=answer, model=model_name))


@router.post(
    "/stream",
    summary="对话（SSE 流式）",
    description=(
        "以 text/event-stream 逐块返回：event=delta 为增量文本，"
        "event=meta 携带模型与用量，event=done 表示结束，event=error 携带错误码。"
        "流式接口不套统一响应结构 —— 它本身是事件流，不是单个 JSON。"
    ),
    response_class=EventSourceResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"},
        422: {"description": "参数校验失败"},
        503: {"description": "LLM 未配置"},
    },
)
async def chat_stream(
    request: Request,
    payload: ChatRequest,
    service: LLMService = Depends(get_llm_service),
    settings: Settings = Depends(get_settings),
) -> EventSourceResponse:
    # 配置缺失属于请求前置条件，必须在响应头下发前拦掉。
    # 这里抛出的 AppError 会被全局处理器接住 —— 此时流还没开始。
    service.ensure_configured()

    return EventSourceResponse(
        _event_source(service, payload),
        # ping 是 SSE 注释行，客户端忽略；用来防住代理的空闲超时
        ping=settings.SSE_PING_SECONDS,
        # 停机时通知在途流收尾，而不是被进程退出硬切断
        shutdown_event=getattr(request.app.state, "sse_shutdown", None),
    )


async def _event_source(
    service: LLMService,
    payload: ChatRequest,
) -> AsyncIterator[ServerSentEvent]:
    try:
        async for item in service.astream_chat(payload):
            if isinstance(item, TokenDelta):
                # 用 JSONServerSentEvent：普通 ServerSentEvent 对 dict 走 str()，
                # 会产出单引号的 Python repr，客户端 JSON.parse 直接失败
                yield JSONServerSentEvent(
                    data=StreamChunk(delta=item.text).model_dump(), event=EVENT_DELTA
                )
            else:
                yield JSONServerSentEvent(data=_meta(item).model_dump(), event=EVENT_META)
        yield ServerSentEvent(data=SSE_DONE, event=EVENT_DONE)
    except asyncio.CancelledError:
        # 客户端断开：静默结束，不再写任何帧
        raise
    except (LLMUpstreamError, ServiceError) as exc:
        # 流已开始，状态码改不了了，只能用事件传递。
        # code 用业务错误码而不是裸 HTTP 状态码，和 JSON 接口保持一致。
        yield JSONServerSentEvent(
            data=StreamError(code=exc.error_code.code, message=exc.message).model_dump(),
            event=EVENT_ERROR,
        )


def _meta(finished: StreamFinished) -> StreamMeta:
    return StreamMeta(
        model=finished.model,
        prompt_tokens=finished.prompt_tokens,
        completion_tokens=finished.completion_tokens,
    )


@router.get(
    "/status",
    response_model=ApiResponse[ChatStatusResponse],
    summary="对话功能状态",
    description="探测 LLM 是否已配置、当前使用哪版提示词，不打真网络。",
)
async def chat_status(
    service: LLMService = Depends(get_llm_service),
) -> ApiResponse[ChatStatusResponse]:
    prompt = service.prompt
    return ok(
        ChatStatusResponse(
            configured=service.configured,
            model=service.model,
            base_url=service.base_url,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
        )
    )
