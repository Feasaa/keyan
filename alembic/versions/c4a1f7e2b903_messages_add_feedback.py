"""messages add feedback columns for thumbs up/down

Revision ID: c4a1f7e2b903
Revises: bba12eb8aaf2
Create Date: 2026-10-28 21:05:00.000000

对话评价（赞 / 踩）落在 messages 行上，不单开一张表：

* 一条消息最多一个评价，1:1 关系没有拆表的必要；
* 历史消息列表要回显"这条赞过没"，独立表就得每页多一次 JOIN 或 IN 查询，
  而列表本来已经是分页查询，再叠一层很别扭；
* 代价是**没有评价历史** —— 用户改主意会覆盖旧值。真需要审计时再加
  `message_feedback_events` 表，那时旧值也能从事件流里补出来。

三列拆开而不是一个 JSON 字段：
    JSON 里的值没法建索引、没法写 CHECK，运营想查"所有差评"只能全表扫。
    feedback 用 TINYINT 存枚举 + CHECK 约束，是这张表其他状态列的既有做法。

feedback 的 0 表示"没评价过"，不是"中立" —— 统计满意度时分母只算评价过的，
否则好评率会被大量没表态的消息稀释成一个永远接近 50% 的假数字。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "c4a1f7e2b903"
down_revision: str | Sequence[str] | None = "bba12eb8aaf2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TINYINT_UNSIGNED = sa.SmallInteger().with_variant(mysql.TINYINT(unsigned=True), "mysql")
TIMESTAMP = sa.DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")


def upgrade() -> None:
    """Upgrade schema."""
    # server_default 必须给：NOT NULL 且无默认值时，存量行无法满足约束，
    # MySQL 会直接拒绝这次 DDL（和 auth_sessions.family_id 那次一样）。
    op.add_column(
        "messages",
        sa.Column(
            "feedback",
            TINYINT_UNSIGNED,
            nullable=False,
            server_default=sa.text("0"),
            comment="用户评价：0 未评价 / 1 赞 / 2 踩",
        ),
    )
    op.add_column(
        "messages",
        sa.Column("feedback_reason", sa.String(255), nullable=True, comment="点踩原因，可选"),
    )
    op.add_column(
        "messages",
        sa.Column("feedback_at", TIMESTAMP, nullable=True, comment="评价时间，取消评价时清空"),
    )
    op.create_check_constraint("ck_msg_feedback", "messages", "feedback IN (0, 1, 2)")
    op.create_index("idx_msg_feedback", "messages", ["feedback", "feedback_at"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("idx_msg_feedback", table_name="messages")
    op.drop_constraint("ck_msg_feedback", "messages", type_="check")
    op.drop_column("messages", "feedback_at")
    op.drop_column("messages", "feedback_reason")
    op.drop_column("messages", "feedback")
