"""数据库会话：引擎、会话工厂、FastAPI 依赖。

关于事务边界 —— 这里**故意不在请求结束时自动 commit**。
    边界由 service 显式决定：该提交的时候 commit，其余交给调用方。
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


def _patch_aiomysql_escape() -> None:
    """补上 aiomysql 与新版 PyMySQL 的转义兼容。

    PyMySQL 1.2 起把 `converters.escape_bytes_prefixed` 换成了**字符串占位符**
    （值就是 "DO NOT IMPORT THIS!!!"），目的是让 aiomysql 的 `from ... import`
    不报 ImportError；但 aiomysql 0.3.2 仍然把它当函数调用，于是任何带 bytes
    参数的 SQL —— 刷新令牌哈希这类列 —— 都会在 INSERT 时抛：

        TypeError: 'str' object is not callable

    这种错误只在真连 MySQL 时出现，用 SQLite 跑的测试完全发现不了。
    所以启动时补一次：名字不可调用就换成 PyMySQL 现在真正提供的 escape_bytes。
    """
    try:
        import aiomysql.connection as aiomysql_connection
        from pymysql import converters
    except ImportError:
        # 非 MySQL 驱动（比如测试用 SQLite）直接跳过
        return

    if callable(getattr(converters, "escape_bytes_prefixed", None)):
        return

    replacement = getattr(converters, "escape_bytes", None)
    if not callable(replacement):
        # 两个名字都不可用时不自造转义实现：转义写错是注入风险，
        # 让它在启动阶段显式失败，比在生产里悄悄拼错 SQL 强。
        raise RuntimeError(
            "PyMySQL 与 aiomysql 版本不兼容（escape_bytes_prefixed 不可调用），"
            '请安装 PyMySQL<1.2：pip install "PyMySQL>=1.1,<1.2"'
        )

    # aiomysql 里是 `from ... import escape_bytes_prefixed`，
    # 模块级名字已经指向那个字符串，两个位置都得换
    converters.escape_bytes_prefixed = replacement
    aiomysql_connection.escape_bytes_prefixed = replacement


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """进程级引擎：内部持有连接池，绝不能每请求新建。

    每请求一个引擎 = 每请求一个连接池 = 连接数爆炸，
    很快就能把 MySQL 的 max_connections 打满。
    """
    settings = get_settings()
    if not settings.db_configured:
        raise RuntimeError("数据库未配置：请在 .env 里设置 DB_PASSWORD")

    _patch_aiomysql_escape()

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
