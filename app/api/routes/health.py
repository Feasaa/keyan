"""健康检查端点：存活探针（liveness）与就绪探针（readiness）。

两个探针分开，是因为它们的失败语义不同：
    liveness  失败 → 编排系统重启容器
    readiness 失败 → 仅从负载均衡摘除，不重启
合并成一个接口会导致"依赖抖动就重启实例"，把小故障放大成雪崩。

响应也套统一结构。探针只看 HTTP 状态码，不看 body，
所以包一层不影响任何编排系统；换来的是全站只有一个响应格式。
"""

from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.response import ApiResponse, ok
from app.schemas.health import HealthResponse, ReadinessResponse
from app.services.llm_service import LLMService, get_llm_service

router = APIRouter()


@router.get("", response_model=ApiResponse[HealthResponse], summary="存活探针（liveness）")
async def liveness() -> ApiResponse[HealthResponse]:
    """只回答"进程活着、应用装配正常"，不查任何外部依赖。"""
    return ok(
        HealthResponse(
            status="ok",
            name=settings.PROJECT_NAME,
            version=settings.VERSION,
        )
    )


@router.get(
    "/ready",
    response_model=ApiResponse[ReadinessResponse],
    summary="就绪探针（readiness）",
)
async def readiness(
    service: LLMService = Depends(get_llm_service),
) -> ApiResponse[ReadinessResponse]:
    """检查对外能力是否可用。

    LLM 未配置时返回 degraded 而非失败：服务仍能提供健康检查等能力，
    对话接口会明确返回 503。若业务上"无密钥即不可用"，把这里改成
    503 并让编排系统摘流量即可。
    """
    configured = service.configured
    return ok(
        ReadinessResponse(
            status="ready" if configured else "degraded",
            checks={"llm": "configured" if configured else "not_configured"},
        )
    )
