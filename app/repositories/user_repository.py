"""用户表的读写。

所有查询都走 ORM 表达式，没有一个字符串拼 SQL —— 见本包 `__init__` 的说明。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import get_snowflake
from app.db.base import utcnow
from app.db.enums import UserStatus
from app.db.models import User


class UserRepository:
    """一个实例绑定一个 AsyncSession，生命周期跟随请求。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_email(self, email: str) -> User | None:
        """按邮箱查有效用户。

        `deleted_at.is_(None)` 是必须的：软删除的行还在表里，
        不排除就会出现"注销过的账号还能登录"。
        把它写进仓储而不是让调用方自己加，是因为漏加一次就是一个安全漏洞，
        而且漏了不会有任何报错。
        """
        stmt = select(User).where(
            User.email == email,
            User.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def get_by_id(self, user_id: int) -> User | None:
        stmt = select(User).where(
            User.id == user_id,
            User.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt)

    async def email_exists(self, email: str) -> bool:
        """只查主键，不取整行 —— 这个判断会被注册前的预检查频繁调用。"""
        stmt = select(User.id).where(
            User.email == email,
            User.deleted_at.is_(None),
        )
        return await self._session.scalar(stmt) is not None

    def add(self, *, email: str, password_hash: str, nickname: str) -> User:
        """构造并挂入会话，由调用方决定何时 flush / commit。

        ID 在应用侧生成（雪花），不依赖数据库自增：
        自增 ID 会在 flush 之后才拿到，而流式对话要在写库前就把 ID 返回给客户端。
        """
        user = User(
            id=get_snowflake().next_id(),
            email=email,
            password_hash=password_hash,
            nickname=nickname or email.split("@", 1)[0],
            status=UserStatus.ACTIVE,
        )
        self._session.add(user)
        return user

    def touch_login(self, user: User, *, ip: bytes | None) -> None:
        """记录登录时间与来源 IP，供风控与"最近登录设备"使用。"""
        user.last_login_at = utcnow()
        user.last_login_ip = ip
