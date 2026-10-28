"""数据库会话：引擎、会话工厂、FastAPI 依赖。

关于事务边界 —— 这里**故意不在请求结束时自动 commit**。
    Spring 里 `@Transactional` 标在 service 方法上，边界是显式的；
    这里同理：由 service/repository 决定什么时候提交。
    原因是流式对话要在不同时刻提交多次（每 2 秒 checkpoint 一次，
    收尾再提交终态），"请求结束统一提交"这套根本不适用。
    自动提交还会把"忘了 commit"变成"看起来成功了"，排查代价更高。

    依赖只负责一件事：出异常时回滚，然后关闭会话。
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """进程级引擎：内部持有连接池，绝不能每请求新建。

    每请求一个引擎 = 每请求一个连接池 = 连接数爆炸，
    很快就能把 MySQL 的 max_connections 打满。
    """
    settings = get_settings()
    if not settings.db_configured:
        raise RuntimeError("数据库未配置：请在 .env 里设置 DB_PASSWORD")

    return create_async_engine(
        settings.database_url,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        # 取连接前先 ping 一下。MySQL 默认 wait_timeout 8 小时，
        # 池里躺久的连接会被服务端单方面关掉，不 ping 就会偶发
        # "MySQL server has gone away"，且只在低峰期出现，极难复现。
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(),
        # expire_on_commit=False：默认 True 时 commit 会让所有属性过期，
        # 之后再访问属性会触发一次同步 IO，在 async 里直接抛
        # MissingGreenlet。这是异步 ORM 最常见的坑。
        expire_on_commit=False,
        # 关掉自动 flush：让"什么时候发 SQL"由代码显式控制，
        # 避免查询时被隐式 flush 打断，也便于看清事务边界。
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：注入一个 AsyncSession。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """停机时释放连接池，由 lifespan 调用。"""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
