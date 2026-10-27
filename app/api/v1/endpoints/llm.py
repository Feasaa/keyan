"""LLM 对话接口：取参数 → 调 service → 异常翻译为状态码。

503 未配置密钥，502 上游异常，422 参数校验失败。
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.schemas.llm import ChatRequest, ChatResponse, LLMStatusResponse
from app.services.exceptions import (
    LLMNotConfiguredError,
    LLMUpstreamError,
    ServiceError,
)
from app.services.llm_service import LLMService, get_llm_service

router = APIRouter()


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="LLM 对话（LangChain）",
    description="单轮/多轮对话。历史只接受 user/assistant 角色；未配置密钥返回 503。",
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


@router.get(
    "/status",
    response_model=LLMStatusResponse,
    summary="LLM 功能状态",
    description="探测 LLM 是否已配置，不打真网络。",
)
async def llm_status(
    service: LLMService = Depends(get_llm_service),
) -> LLMStatusResponse:
    return LLMStatusResponse(
        configured=service.configured,
        model=service.model,
        base_url=service.base_url,
    )
