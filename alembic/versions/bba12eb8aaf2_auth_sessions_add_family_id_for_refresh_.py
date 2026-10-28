"""auth_sessions add family_id for refresh token reuse detection

Revision ID: bba12eb8aaf2
Revises: 989f5968f21b
Create Date: 2026-10-28 16:32:26.494627

refresh token 轮换后，旧 token 被再次使用只有两种可能：
并发重试，或者 token 被复制走了。后者说明整条链路都已泄露，
需要把同一条链路（family）上的会话一次性全撤。

分三步加列，不能一步到位：
    ADD COLUMN ... NOT NULL 在已有数据的表上会被 MySQL 拒绝
    （没有默认值，存量行无法满足约束）。
    先加可空列 → 回填 → 再收紧成 NOT NULL。

回填值用 id 而不是 0：填 0 会让所有存量会话共用一个 family，
将来一旦触发复用检测，会把所有人一起踢下线。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "bba12eb8aaf2"
down_revision: str | Sequence[str] | None = "989f5968f21b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_UNSIGNED = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "auth_sessions",
        sa.Column("family_id", BIGINT_UNSIGNED, nullable=True, comment="登录链路 ID"),
    )
    # 存量会话各自成一条链路：family 就是自己
    op.execute("UPDATE auth_sessions SET family_id = id WHERE family_id IS NULL")
    op.alter_column(
        "auth_sessions",
        "family_id",
        existing_type=BIGINT_UNSIGNED,
        nullable=False,
        comment="登录链路 ID",
    )
    op.create_index("idx_sess_family", "auth_sessions", ["family_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_sess_family", table_name="auth_sessions")
    op.drop_column("auth_sessions", "family_id")
