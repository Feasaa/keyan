"""健康检查接口：供负载均衡与 K8s 探针轮询，无副作用、无鉴权。"""

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.health import HealthResponse

router = APIRouter()


@router.get("", response_model=HealthResponse, summary="健康检查")
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        name=settings.PROJECT_NAME,
        version=settings.VERSION,
    )
