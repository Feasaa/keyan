"""Alembic 环境：复用应用自己的配置与模型。

两个刻意的选择：
    DSN 不写进 alembic.ini —— ini 是进版本库的，写密码就是泄露；
    而且两份 DSN 必然漂移，最后没人说得清迁移到底连的哪个库。
    target_metadata 直接指向 app.db.Base，不在迁移里另抄一份模型定义。
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from app.core.config import get_settings
from app.db import Base
from app.db.types import IntEnumValue, StrEnumValue
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """让迁移文件只依赖 sqlalchemy，不依赖应用自己的模型代码。

    自定义 TypeDecorator 默认会被渲染成 `app.db.types.IntEnumValue()` ——
    既丢了构造参数，还要求迁移文件 import 应用包。
    后果很隐蔽：模型以后改了名字或构造签名，这些**历史迁移**就跑不起来了，
    而那时你正忙着上线，根本没空去修三年前的迁移。

    所以这里退化成物理类型（TINYINT UNSIGNED / VARCHAR）。
    迁移描述的是"数据库长什么样"，不是"代码怎么表达它"。
    """
    if type_ == "type":
        if isinstance(obj, IntEnumValue):
            autogen_context.imports.add("from sqlalchemy.dialects import mysql")  # type: ignore[attr-defined]
            return "mysql.TINYINT(unsigned=True)"
        if isinstance(obj, StrEnumValue):
            return f"sa.String(length={obj.length})"
    return False


# Alembic 无法可靠比对"表达式索引"。
# idx_conv_user_recent 建在 (user_id, last_message_at DESC) 上，
# DESC 不是列而是一个表达式，Alembic 只能生成"近似签名"，
# 于是每次 check 都报"索引变了"，但实际没变 —— 纯粹的噪声。
# 代价：这个索引的变更要手写迁移，不能再依赖 autogenerate。
UNCOMPARABLE_INDEXES = {"idx_conv_user_recent"}


def include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    if type_ == "index" and name in UNCOMPARABLE_INDEXES:
        return False
    return True


def _database_url() -> str:
    """迁移用 DSN。

    优先用 DB_MIGRATION_USER（DDL 权限），运行时账号 DB_USER 只有 DML ——
    这一层隔离的意义：即使将来出现一处 SQL 注入，攻击者拿到的也只是
    应用账号，删不了表、改不了结构、读不到其他库。
    """
    settings = get_settings()
    if not settings.db_configured:
        raise RuntimeError("数据库未配置：请在 .env 里设置 DB_PASSWORD")
    return settings.migration_url


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连库（用于评审或交给 DBA 执行）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 类型变更也要被 autogenerate 发现。
        # 不开的话，把 VARCHAR(64) 改成 VARCHAR(128) 这类改动会被静默忽略，
        # 迁移写出来是空的，而库里也没变 —— 最难受的一种"看起来成功了"。
        compare_type=True,
        render_item=render_item,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
