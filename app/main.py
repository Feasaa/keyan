"""应用入口：应用工厂 + 生命周期 + 中间件 + 路由装配。

启动：uvicorn app.main:app --reload --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.core.middleware import RequestContextMiddleware
from app.db import close_redis, dispose_engine
from app.prompts import get_prompt_registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时申请昂贵资源；关闭时先通知在途 SSE 流收尾，再释放资源。

    sse_shutdown 由 EventSourceResponse 消费：收到 set 后，进行中的流会
    发一个 shutdown 事件并主动关闭连接，而不是被进程退出硬切断。
    """
    # 提示词在包外的 PROMPTS_DIR 里（容器通常挂载进来），路径配错、
    # 正文写坏都要在启动时就炸掉 —— 否则要等第一个用户请求才 500。
    get_prompt_registry()
    app.state.sse_shutdown = anyio.Event()
    yield
    # 先让在途的流收尾（它们可能还要写库），再释放连接池
    app.state.sse_shutdown.set()
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    """应用工厂：返回配置完毕的实例，便于测试时按需重建。"""
    application = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        description="FastAPI + LangChain 服务骨架",
        docs_url=settings.DOCS_URL,
        redoc_url="/redoc",
        openapi_url=settings.OPENAPI_URL,
        lifespan=lifespan,
    )

    # 生产环境请把 allow_origins 收敛为具体域名
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 后加的在外层：trace_id 要在 CORS 之前拿到，
    # 这样连被 CORS 拒绝的响应也带 X-Request-ID
    application.add_middleware(RequestContextMiddleware)

    register_exception_handlers(application)

    application.include_router(api_router, prefix=settings.API_PREFIX)
    return application


app = create_app()
