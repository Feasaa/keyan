"""模型 ↔ 真实数据库的契约测试。

表结构由 init/01-schema.sql 管理，模型只是它的镜像。
镜像最容易悄悄漂移：改了 SQL 忘了改模型（或反过来），
单元测试全绿，直到某次上线才在运行时炸。
所以这里直接连真实库，拿 information_schema 逐项比对。

数据库没起时自动跳过，不阻塞本地跑单测。
"""

import asyncio
import contextlib
from dataclasses import dataclass

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.snowflake import SnowflakeGenerator
from app.db import Base, Message, MessageRole, MessageStatus, dispose_engine


@dataclass(frozen=True)
class Snapshot:
    columns: dict[str, dict[str, str]]
    indexes: dict[str, set[str]]
    constraints: dict[str, set[str]]


def _normalize_type(type_text: str) -> str:
    """MySQL 的 INT / INTEGER 是同一个类型，information_schema 统一报 int。"""
    return type_text.lower().replace("integer", "int")


async def _load_snapshot() -> Snapshot:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    params = {"db": settings.DB_NAME}
    try:
        async with engine.connect() as conn:
            column_rows = (
                await conn.execute(
                    text(
                        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE "
                        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :db"
                    ),
                    params,
                )
            ).all()
            index_rows = (
                await conn.execute(
                    text(
                        "SELECT DISTINCT TABLE_NAME, INDEX_NAME FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA = :db"
                    ),
                    params,
                )
            ).all()
            constraint_rows = (
                await conn.execute(
                    text(
                        "SELECT TABLE_NAME, CONSTRAINT_NAME "
                        "FROM information_schema.TABLE_CONSTRAINTS "
                        "WHERE TABLE_SCHEMA = :db"
                    ),
                    params,
                )
            ).all()
    finally:
        await engine.dispose()

    columns: dict[str, dict[str, str]] = {}
    for table, column, column_type in column_rows:
        columns.setdefault(table, {})[column] = _normalize_type(column_type)

    indexes: dict[str, set[str]] = {}
    for table, index in index_rows:
        indexes.setdefault(table, set()).add(index)

    constraints: dict[str, set[str]] = {}
    for table, constraint in constraint_rows:
        constraints.setdefault(table, set()).add(constraint)

    return Snapshot(columns=columns, indexes=indexes, constraints=constraints)


@pytest.fixture(scope="module")
def snapshot() -> Snapshot:
    if not get_settings().db_configured:
        pytest.skip("未配置 DB_PASSWORD，跳过数据库契约测试")
    try:
        return asyncio.run(_load_snapshot())
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"数据库不可达：{type(exc).__name__}")


def test_all_model_tables_exist(snapshot: Snapshot) -> None:
    missing = set(Base.metadata.tables) - set(snapshot.columns)
    assert not missing, f"数据库里缺少这些表：{missing}（先跑 deploy 的 init 脚本）"


def test_columns_match_exactly(snapshot: Snapshot) -> None:
    """双向比对：模型多一列、少一列都算漂移。"""
    for table_name, table in Base.metadata.tables.items():
        model_columns = {c.name for c in table.columns}
        db_columns = set(snapshot.columns.get(table_name, {}))

        assert model_columns == db_columns, (
            f"{table_name} 列不一致："
            f"模型多出 {model_columns - db_columns}，数据库多出 {db_columns - model_columns}"
        )


def test_column_types_match(snapshot: Snapshot) -> None:
    """物理类型必须一致 —— 差一个 UNSIGNED，取值范围就差一倍。"""
    from sqlalchemy.dialects import mysql

    dialect = mysql.dialect()
    mismatches = []

    for table_name, table in Base.metadata.tables.items():
        db_columns = snapshot.columns.get(table_name, {})
        for column in table.columns:
            if column.name not in db_columns:
                continue
            model_type = _normalize_type(column.type.compile(dialect=dialect))
            db_type = db_columns[column.name]
            if model_type != db_type:
                mismatches.append(f"{table_name}.{column.name}: 模型={model_type} 库={db_type}")

    assert not mismatches, "类型漂移：\n" + "\n".join(mismatches)


def test_indexes_exist(snapshot: Snapshot) -> None:
    missing = []
    for table_name, table in Base.metadata.tables.items():
        db_indexes = snapshot.indexes.get(table_name, set())
        for index in table.indexes:
            if index.name not in db_indexes:
                missing.append(f"{table_name}.{index.name}")

    assert not missing, f"数据库缺少这些索引：{missing}"


def test_constraints_exist(snapshot: Snapshot) -> None:
    """PRIMARY 除外：MySQL 强制主键约束叫 PRIMARY，模型里叫什么都会被忽略。"""
    missing = []
    for table_name, table in Base.metadata.tables.items():
        db_constraints = snapshot.constraints.get(table_name, set())
        for constraint in table.constraints:
            name = constraint.name
            if not name or name.startswith("pk_") or name == "PRIMARY":
                continue
            if name not in db_constraints:
                missing.append(f"{table_name}.{name}")

    assert not missing, f"数据库缺少这些约束：{missing}"


# ------------------------------------------------------------------ 真实读写


async def _roundtrip() -> tuple[str | None, MessageRole, MessageStatus]:
    """在一个事务里写入并读回，最后回滚 —— 不污染开发库。

    验证的是 TypeDecorator 的绑定与还原：写进库是 'user' / 0，
    读出来必须是枚举成员，而不是裸字符串。
    """
    from app.db import Conversation, User

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    generator = SnowflakeGenerator(worker_id=0)
    try:
        async with factory() as session:
            user_id, conv_id, msg_id = (generator.next_id() for _ in range(3))
            session.add(
                User(id=user_id, username="contract_probe", password_hash="x", nickname="探针")
            )
            session.add(Conversation(id=conv_id, user_id=user_id, title="契约测试"))
            message = Message(
                id=msg_id,
                conversation_id=conv_id,
                user_id=user_id,
                seq=1,
                role=MessageRole.USER,
                content="你好，世界",
            )
            session.add(message)
            await session.flush()
            await session.refresh(message)

            result = (message.content, message.role, message.status)
            await session.rollback()
            return result
    finally:
        await engine.dispose()


def test_write_and_read_roundtrip() -> None:
    if not get_settings().db_configured:
        pytest.skip("未配置 DB_PASSWORD，跳过数据库契约测试")
    try:
        content, role, status = asyncio.run(_roundtrip())
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"数据库不可达：{type(exc).__name__}")

    assert content == "你好，世界"
    assert role is MessageRole.USER
    assert status is MessageStatus.STREAMING


async def _write_without_commit() -> bool:
    """走一遍 get_session 依赖：写完不提交就退出，再看库里有没有。

    全程在同一个事件循环里完成。跨 asyncio.run 复用全局引擎会踩到
    "连接绑在已关闭的 loop 上"，最后以 Connection.__del__ 的形式
    在毫不相干的测试里冒出来。
    """
    from app.db import User, get_session, get_session_factory

    generator = get_session()
    session = await anext(generator)
    user_id = SnowflakeGenerator(worker_id=0).next_id()
    session.add(User(id=user_id, username=f"no_commit_{user_id}", password_hash="x"))
    await session.flush()
    # 正常结束生成器：依赖关闭会话，未提交的事务被回滚
    with contextlib.suppress(StopAsyncIteration):
        await anext(generator)

    try:
        async with get_session_factory()() as fresh:
            return await fresh.get(User, user_id) is not None
    finally:
        await dispose_engine()


def test_session_dependency_does_not_autocommit() -> None:
    """依赖只负责回滚和关闭，提交由 service 显式决定。

    这条约定不锁住的话，很容易有人顺手在依赖里加个 commit，
    于是"某段业务忘了提交"变成"看起来成功了"，
    而流式对话需要在不同时刻提交多次，统一提交的语义根本对不上。
    """
    if not get_settings().db_configured:
        pytest.skip("未配置 DB_PASSWORD，跳过数据库契约测试")
    try:
        exists = asyncio.run(_write_without_commit())
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"数据库不可达：{type(exc).__name__}")

    assert exists is False, "get_session 不应自动提交"
