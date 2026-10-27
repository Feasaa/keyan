"""路由聚合：main.py 只挂载一次，新增资源只改这里。

版本号留在 URL 契约里（/api/v1），不再用目录嵌套表达 ——
目录结构属于内部实现，改版不应导致整包搬家。
"""

from fastapi import APIRouter

from app.controllers import chat, health

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(chat.router, prefix="/chat", tags=["对话"])
