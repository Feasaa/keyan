"""users.username nullable for email-only signup

注册改成邮箱制之后，"用户名"这个字段对邮箱用户没有意义 ——
硬填一个（比如取邮箱前缀）会和"用户以后改邮箱"打架，
而且前缀撞车时还要编造去重规则。让它是 NULL 更诚实。

MySQL 的 UNIQUE 索引允许多个 NULL，所以 uk_users_username 不会因为
"一批用户都没有用户名"而互相冲突（换成空字符串就会撞）。

Revision ID: 989f5968f21b
Revises: 9763bcc037db
Create Date: 2026-10-28 12:50:02.871237

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "989f5968f21b"
down_revision: str | Sequence[str] | None = "9763bcc037db"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "users",
        "username",
        existing_type=mysql.VARCHAR(length=64),
        nullable=True,
        comment="登录账号；邮箱注册的用户为 NULL",
        existing_comment="登录账号",
    )


def downgrade() -> None:
    """Downgrade schema.

    注意：回滚前必须先清理 username IS NULL 的行，
    否则 MySQL 会把 NULL 当成空串写入（严格模式下直接报 1138），
    接着撞 uk_users_username。这一步刻意不做自动处理 ——
    删数据这种事不该藏在 downgrade 里。
    """
    op.alter_column(
        "users",
        "username",
        existing_type=mysql.VARCHAR(length=64),
        nullable=False,
        comment="登录账号",
        existing_comment="登录账号；邮箱注册的用户为 NULL",
    )
