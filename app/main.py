"""应用入口：应用工厂 + 生命周期 + 中间件 + 路由装配。

启动：uvicorn app.main:app --reload --port 8000
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import Settings, settings
from app.core.exception_handlers import register_exception_handlers
from app.core.middleware import TRACE_ID_HEADER, RequestContextMiddleware
from app.db import close_redis, dispose_engine
from app.prompts import get_prompt_registry

logger = logging.getLogger(__name__)


def _resolve_cors(config: Settings) -> tuple[list[str], bool]:
    """把 CORS 配置解析成 (origins, allow_credentials)。

    为什么有通配符时必须关掉凭证：
        CORS 规范禁止 `Access-Control-Allow-Origin: *` 与
        `Access-Control-Allow-Credentials: true` 并存。Starlette 的处理是
        **把请求的 Origin 原样回显**（见 cors.py 的 `allow_explicit_origin`），
        于是响应头在语法上是合法的、浏览器也照收 —— 问题不在"被拒绝"，
        而在"被放行"：既然所有 Origin 都允许，回显就等于任何站点都能发起
        带凭证的跨域请求并读到响应内容。

        本项目目前用 `Authorization: Bearer`，凭证不在浏览器里自动携带，
        危害有限；但只要哪天有人改成 Cookie 会话，这一条立刻变成
        "任意网站可读取登录用户的全部接口"。所以要在它还是隐患时就堵掉。

    结论：`*` 与 credentials 二选一，有 `*` 就关 credentials。
    真要带凭证，就把具体域名写进 CORS_ORIGINS。
    """
    origins = list(config.CORS_ORIGINS)
    if "*" not in origins:
        return origins, True

    logger.warning(
        "CORS_ORIGINS 含通配符 '*'，已自动关闭 allow_credentials："
        "否则中间件会回显任意 Origin，等于允许所有站点发起带凭证的跨域请求。"
        "需要凭证请把具体域名写进 CORS_ORIGINS。"
    )
    return origins, False


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

    origins, allow_credentials = _resolve_cors(settings)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        # 前端要读 trace_id 去对服务端日志，默认不暴露的话 JS 拿不到
        expose_headers=[TRACE_ID_HEADER],
    )

    # 后加的在外层：trace_id 要在 CORS 之前拿到，
    # 这样连被 CORS 拒绝的响应也带 X-Request-ID
    application.add_middleware(RequestContextMiddleware)

    register_exception_handlers(application)

    application.include_router(api_router, prefix=settings.API_PREFIX)
    return application


app = create_app()
