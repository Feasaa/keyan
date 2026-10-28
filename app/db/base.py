"""ORM 基类与时间约定。

约束命名约定不是可选项：不写约定的话，MySQL 会自动生成
`messages_ibfk_1` 这类名字，Alembic 每次比对都认为约束变了，
于是反复 drop/create。名字必须可预测，且要和 init/01-schema.sql 里的一致。
"""

from datetime import UTC, datetime

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 所有约束/索引都显式命名（见 models.py），这里的约定只是兜底：
# 万一漏写了 name=，至少生成的还是可读名字，而不是 messages_ibfk_1。
NAMING_CONVENTION = {
    "ix": "idx_%(table_name)s_%(column_0_N_name)s",
    "uq": "uk_%(table_name)s_%(column_0_N_name)s",
    # ck 这里必须是纯占位符，不能加前缀。
    # 因为 SQLAlchemy 的约定会在**显式给的 name 外面再套一层**：
    # name="ck_msg_role" + 约定 "ck_%(table_name)s_%(constraint_name)s"
    # 会拼成 ck_messages_ck_msg_role，与 init SQL 里的名字对不上，
    # 迁移工具就会认为约束被改过。纯占位符等于"用我写的名字，别动它"，
    # 顺带强制每个 CHECK 都必须显式命名。
    "ck": "%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """当前 UTC 时间（naive）。

    MySQL 的 DATETIME 不存时区，所以这里去掉 tzinfo：
    传 aware datetime 进去，驱动会静默剥掉时区，
    之后读出来的值到底是 UTC 还是本地时间就说不清了。
    约定比聪明更重要 —— 全链路统一 naive UTC。
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
