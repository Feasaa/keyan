"""登录态（refresh token）的读写。

表里只存 `sha256(token)`，不存原文：库被读走也无法直接冒用。
查询时按哈希查，所以"拿 token 找会话"和"按主键找"一样快（uk_sess_token）。
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import get_snowflake
from app.db.base import utcnow
from app.db.models import AuthSession

# user_agent 列是 VARCHAR(255)。超长在 MySQL 严格模式下会直接报
# 1406 Data too long，而不是静默截断 —— 一个伪造的超长 UA 就能让登录 500。
# 所以在入口处截断，别指望数据库宽容。
USER_AGENT_MAX_LENGTH = 255


class AuthSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(
        self,
        *,
        user_id: int,
        token_hash: str,
        expires_at: datetime,
        ip: bytes | None,
        user_agent: str,
    ) -> AuthSession:
        record = AuthSession(
            id=get_snowflake().next_id(),
            user_id=user_id,
            refresh_token_hash=token_hash,
            expires_at=expires_at,
            ip=ip,
            user_agent=(user_agent or "")[:USER_AGENT_MAX_LENGTH],
        )
        self._session.add(record)
        return record

    async def get_by_token_hash(self, token_hash: str) -> AuthSession | None:
        """只返回**仍然有效**的会话。

        把"已撤销"和"已过期"过滤在 SQL 里，而不是查出来再用 Python 判断：
        前者是索引扫描，后者要把整个用户的历史会话都读进内存。
        """
        stmt = select(AuthSession).where(
            AuthSession.refresh_token_hash == token_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > utcnow(),
        )
        return await self._session.scalar(stmt)

    async def get_by_id(self, session_id: int) -> AuthSession | None:
        stmt = select(AuthSession).where(AuthSession.id == session_id)
        return await self._session.scalar(stmt)

    def revoke(self, record: AuthSession) -> None:
        """标记失效。用时间戳而不是删行：

        删行后"这个 token 曾经存在过、现在被撤销了"这个事实就丢了，
        无法区分"token 是伪造的"和"token 是真但已被换掉"。
        后者往往意味着 refresh token 被盗用，是应该告警的信号。
        """
        if record.revoked_at is None:
            record.revoked_at = utcnow()

    async def revoke_all_for_user(self, user_id: int) -> int:
        """踢掉某用户的所有登录态（改密、封号、疑似盗用时调用）。"""
        now = utcnow()
        stmt = select(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
        )
        records = list((await self._session.scalars(stmt)).all())
        for record in records:
            record.revoked_at = now
        return len(records)
