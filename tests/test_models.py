"""模型元数据测试：不连数据库，只锁住"生成的 DDL 是什么样"。

这些断言的价值在于拦住"改模型时不小心改了物理类型"——
比如把 BIGINT UNSIGNED 改成 BigInteger（有符号），
本地跑起来一切正常，上线后 ID 超过 2^63 才炸。
"""

import pytest
from sqlalchemy.dialects import mysql

from app.core.config import Settings
from app.db import Base
from app.db.base import utcnow
from app.db.enums import MessageRole, MessageStatus, UserStatus
from app.db.types import IntEnumValue, StrEnumValue

MYSQL = mysql.dialect()


def _compiled_type(column) -> str:
    return column.type.compile(dialect=MYSQL)


def test_expected_tables_registered() -> None:
    assert set(Base.metadata.tables) == {
        "users",
        "conversations",
        "messages",
        "auth_sessions",
    }


@pytest.mark.parametrize("table_name", ["users", "conversations", "messages", "auth_sessions"])
def test_primary_key_is_snowflake(table_name: str) -> None:
    """主键必须是 BIGINT UNSIGNED 且不自增 —— 雪花 ID 由应用侧生成。"""
    table = Base.metadata.tables[table_name]
    (pk_column,) = table.primary_key.columns

    assert pk_column.name == "id"
    assert _compiled_type(pk_column) == "BIGINT UNSIGNED"
    assert pk_column.autoincrement in (False, "auto")


def test_messages_physical_types() -> None:
    """正文用 MEDIUMTEXT（TEXT 只有 64KB，长回答会被截断）。"""
    columns = Base.metadata.tables["messages"].columns

    assert _compiled_type(columns["content"]) == "MEDIUMTEXT"
    assert _compiled_type(columns["seq"]) == "INTEGER UNSIGNED"
    assert _compiled_type(columns["status"]) == "TINYINT UNSIGNED"
    assert _compiled_type(columns["created_at"]) == "DATETIME(3)"


def test_ip_columns_are_binary() -> None:
    """IP 存 VARBINARY(16)，不存字符串 —— IPv6 最长 45 字符，索引会很宽。"""
    assert _compiled_type(Base.metadata.tables["users"].columns["last_login_ip"]) == "VARBINARY(16)"
    assert _compiled_type(Base.metadata.tables["auth_sessions"].columns["ip"]) == "VARBINARY(16)"


def test_constraint_names_match_init_sql() -> None:
    """约束名必须和 init/01-schema.sql 逐字一致，否则迁移工具会反复重建。

    PRIMARY KEY 除外：MySQL 强制把主键约束命名为 PRIMARY，
    模型里叫什么都会被忽略。
    """
    expected = {
        "users": {"uk_users_username", "uk_users_email", "ck_users_status"},
        "conversations": {"fk_conv_user", "ck_conv_status"},
        "messages": {
            "uk_msg_conv_seq",
            "uk_msg_client_idem",
            "fk_msg_conv",
            "ck_msg_role",
            "ck_msg_status",
        },
        "auth_sessions": {"uk_sess_token", "fk_sess_user"},
    }

    for table_name, names in expected.items():
        actual = {
            c.name
            for c in Base.metadata.tables[table_name].constraints
            if c.name and c.name != "PRIMARY" and not c.name.startswith("pk_")
        }
        assert names <= actual, f"{table_name} 缺少约束：{names - actual}"


def test_relationships_are_lazy_raise() -> None:
    """所有关系都是 lazy="raise"。

    异步 ORM 里惰性加载会抛 MissingGreenlet，报错信息完全看不出是哪一行触发的。
    设成 raise 后，忘了 selectinload 会在访问属性时立刻明确报错。
    """
    for mapper in Base.registry.mappers:
        for rel in mapper.relationships:
            assert rel.lazy == "raise", f"{mapper.class_.__name__}.{rel.key} 不是 lazy='raise'"


def test_str_enum_roundtrip() -> None:
    """枚举列写入存字符串，读出还原成枚举成员。"""
    column_type = StrEnumValue(MessageRole)

    assert column_type.process_bind_param(MessageRole.ASSISTANT, MYSQL) == "assistant"
    assert column_type.process_result_value("assistant", MYSQL) is MessageRole.ASSISTANT
    assert column_type.process_bind_param(None, MYSQL) is None
    # 非法值在赋值时就炸，不用等 commit 收到 MySQL 的 3819
    with pytest.raises(ValueError):
        column_type.process_bind_param("hacker", MYSQL)


def test_int_enum_roundtrip() -> None:
    column_type = IntEnumValue(MessageStatus)

    assert column_type.process_bind_param(MessageStatus.STREAMING, MYSQL) == 0
    assert column_type.process_result_value(1, MYSQL) is MessageStatus.COMPLETE
    assert column_type.process_result_value(None, MYSQL) is None
    with pytest.raises(ValueError):
        column_type.process_bind_param(9, MYSQL)


def test_utcnow_is_naive_utc() -> None:
    """MySQL 的 DATETIME 不存时区，写进去的必须是 naive UTC。"""
    now = utcnow()

    assert now.tzinfo is None
    # 与 UTC 当前时间相差不超过 1 分钟（本机时区不是 UTC 时这条才有意义）
    from datetime import UTC, datetime

    delta = abs((datetime.now(UTC).replace(tzinfo=None) - now).total_seconds())
    assert delta < 60


def test_database_url_escapes_special_characters() -> None:
    """密码含 @ : / # 时必须转义，否则 DSN 被拆坏，报的错跟密码毫无关系。"""
    settings = Settings(DB_USER="app", DB_PASSWORD="p@ss:word/#1", DB_HOST="db", DB_PORT=3307)

    url = settings.database_url
    assert "p%40ss%3Aword%2F%231" in url
    assert url.startswith("mysql+aiomysql://app:")
    assert "@db:3307/agent_chat" in url


def test_enum_values_match_check_constraints() -> None:
    """枚举值一旦上线就不能改，历史数据里存的就是这些数字/字符串。"""
    assert [s.value for s in UserStatus] == [0, 1, 2]
    assert [s.value for s in MessageStatus] == [0, 1, 2, 3]
    assert {r.value for r in MessageRole} == {"user", "assistant", "system", "tool"}
