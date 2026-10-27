"""对话端点：一次性响应、SSE 流式响应、配置状态查询。

分层：路由只做参数校验、状态码翻译与协议适配，链路细节全在 LLMService。
SSE 有两种错误通道：
    流开始前 → HTTPException（客户端拿到真实的 503）
    流开始后 → event=error 帧（响应头已发出，无法再改状态码）
"""

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.core.sse import SSE_DONE, format_event, with_heartbeat
from app.schemas.chat import (
    ChatRequest,
    ChatResponse,
    ChatStatusResponse,
    StreamChunk,
    StreamError,
)
from app.services.exceptions import (
    LLMNotConfiguredError,
    LLMUpstreamError,
    ServiceError,
)
from app.services.llm_service import LLMService, get_llm_service

router = APIRouter()

# 关闭各级代理缓冲，否则 SSE 会被攒成整块再下发，流式效果消失
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


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
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE 事件流"},
        422: {"description": "参数校验失败"},
        503: {"description": "LLM 未配置"},
    },
)
async def chat_stream(
    payload: ChatRequest,
    service: LLMService = Depends(get_llm_service),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    # 配置缺失属于请求前置条件，必须在响应头下发前拦掉
    _ensure_configured(service)

    return StreamingResponse(
        _event_source(service, payload, settings.SSE_HEARTBEAT_SECONDS),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _event_source(
    service: LLMService,
    payload: ChatRequest,
    heartbeat_seconds: float,
) -> AsyncIterator[str]:
    try:
        async for delta in with_heartbeat(
            service.astream_chat(payload), heartbeat_seconds
        ):
            yield format_event(StreamChunk(delta=delta).model_dump(), event="delta")
        yield format_event(SSE_DONE, event="done")
    except asyncio.CancelledError:
        # 客户端断开：静默结束，不再写任何帧
        raise
    except LLMUpstreamError as exc:
        yield format_event(
            StreamError(
                code=status.HTTP_502_BAD_GATEWAY, message=str(exc)
            ).model_dump(),
            event="error",
        )
    except ServiceError:
        yield format_event(
            StreamError(
                code=status.HTTP_502_BAD_GATEWAY, message="LLM 服务内部异常"
            ).model_dump(),
            event="error",
        )


@router.get(
    "/status",
    response_model=ChatStatusResponse,
    summary="对话功能状态",
    description="探测 LLM 是否已配置，不打真网络。",
)
async def chat_status(
    service: LLMService = Depends(get_llm_service),
) -> ChatStatusResponse:
    return ChatStatusResponse(
        configured=service.configured,
        model=service.model,
        base_url=service.base_url,
    )
