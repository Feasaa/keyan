"""v1 路由聚合：新增资源只改这里。"""

from fastapi import APIRouter

from app.api.v1.endpoints import health, llm

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(llm.router, prefix="/llm", tags=["LLM 对话"])
