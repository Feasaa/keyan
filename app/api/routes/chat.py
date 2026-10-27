"""对话端点：一次性响应、SSE 流式响应、配置状态查询。

分层：路由只做参数校验、状态码翻译与协议适配，链路细节全在 LLMService。
SSE 有两种错误通道：
    流开始前 → HTTPException（客户端拿到真实的 503）
    流开始后 → event=error 帧（响应头已发出，无法再改状态码）
帧编码、ping 心跳、断线取消、停机收尾都由 sse-starlette 处理。
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette import EventSourceResponse, JSONServerSentEvent, ServerSentEvent

from app.core.config import Settings, get_settings
from app.core.sse import EVENT_DELTA, EVENT_DONE, EVENT_ERROR, EVENT_META, SSE_DONE
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ChatStatusResponse,
    StreamChunk,
    StreamError,
    StreamMeta,
)
from app.services.exceptions import (
    LLMNotConfiguredError,
    LLMUpstreamError,
    ServiceError,
)
from app.services.llm_service import (
    LLMService,
    StreamFinished,
    TokenDelta,
    get_llm_service,
)

router = APIRouter()


def _ensure_configured(service: LLMService) -> None:
    try:
        service.ensure_configured()
    except LLMNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@router.post(
    "",
    response_model=ChatResponse,
    summary="对话（一次性返回）",
    description="等待模型生成完毕后一次性返回，适合短回答与服务端调用。",
)
async def chat(
    payload: ChatRequest,
    service: LLMService = Depends(get_llm_service),
) -> ChatResponse:
    try:
        answer, model_name = service.chat(payload)
    except LLMNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except LLMUpstreamError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except ServiceError as exc:
        # 兜底：同样不透传原始信息
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LLM 服务内部异常",
        ) from exc

    return ChatResponse(answer=answer, model=model_name)


@router.post(
    "/stream",
    summary="对话（SSE 流式）",
    description=(
        "以 text/event-stream 逐块返回：event=delta 为增量文本，"
        "event=done 携带 [DONE] 表示结束，event=error 携带错误码。"
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
    # 配置缺失属于请求前置条件，必须在响应头下发前拦掉
    _ensure_configured(service)

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
    except LLMUpstreamError as exc:
        yield JSONServerSentEvent(
            data=StreamError(code=status.HTTP_502_BAD_GATEWAY, message=str(exc)).model_dump(),
            event=EVENT_ERROR,
        )
    except ServiceError:
        yield JSONServerSentEvent(
            data=StreamError(
                code=status.HTTP_502_BAD_GATEWAY, message="LLM 服务内部异常"
            ).model_dump(),
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
    response_model=ChatStatusResponse,
    summary="对话功能状态",
    description="探测 LLM 是否已配置、当前使用哪版提示词，不打真网络。",
)
async def chat_status(
    service: LLMService = Depends(get_llm_service),
) -> ChatStatusResponse:
    prompt = service.prompt
    return ChatStatusResponse(
        configured=service.configured,
        model=service.model,
        base_url=service.base_url,
        prompt_id=prompt.id,
        prompt_version=prompt.version,
    )
