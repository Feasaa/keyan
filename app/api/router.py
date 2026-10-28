"""路由聚合：main.py 只挂载一次，新增资源只改这里。

版本号留在 URL 契约里（/api/v1），不用目录嵌套表达 ——
目录结构属于内部实现，改版不应导致整包搬家。
"""

from fastapi import APIRouter

from app.api.routes import auth, chat, conversations, health, messages

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["鉴权"])
api_router.include_router(health.router, prefix="/health", tags=["健康检查"])
api_router.include_router(chat.router, prefix="/chat", tags=["对话"])
# 会话是对话的"容器"，两级资源分开挂：/conversations/{id}/messages 读历史，
# /messages/{id}/feedback 写评价 —— 评价的对象是消息，不是会话
api_router.include_router(conversations.router, prefix="/conversations", tags=["会话"])
api_router.include_router(messages.router, prefix="/messages", tags=["消息"])
