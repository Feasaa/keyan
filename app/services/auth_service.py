"""注册 / 登录 / 刷新 / 登出。

事务边界在这里，不在依赖里 —— 见 app/db/session.py 的说明。
每个方法自己决定什么时候 commit，因为"建号 + 签发登录态"必须一起成功：
只建了号没发令牌，用户会以为注册失败，重试却撞"邮箱已注册"，直接卡死。
"""

import logging
from datetime import timedelta
from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import (
    create_access_token,
    dummy_hash,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.db.base import utcnow
from app.db.enums import UserStatus
from app.db.models import User
from app.repositories import AuthSessionRepository, UserRepository
from app.schemas.auth import VerificationScene
from app.services.exceptions import (
    AccountDisabledError,
    AuthNotConfiguredError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    SessionInvalidError,
)
from app.services.verification_service import VerificationService
from app.utils.masking import mask_email
from app.utils.net import pack_ip

logger = logging.getLogger(__name__)


class IssuedTokens(NamedTuple):
    access_token: str
    refresh_token: str
    expires_in: int


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        verification: VerificationService,
        settings: Settings,
    ) -> None:
        self._session = session
        self._verification = verification
        self._settings = settings
        self._users = UserRepository(session)
        self._sessions = AuthSessionRepository(session)

    # ------------------------------------------------------------------ 发码

    async def send_register_code(
        self, *, email: str, scene: VerificationScene, client_ip: str | None
    ) -> tuple[int, int]:
        self._ensure_configured()
        return await self._verification.issue(email=email, scene=scene.value, client_ip=client_ip)

    # ------------------------------------------------------------------ 注册

    async def register(
        self,
        *,
        email: str,
        password: str,
        code: str,
        nickname: str,
        client_ip: str | None,
        user_agent: str,
    ) -> tuple[User, IssuedTokens]:
        self._ensure_configured()

        # 顺序很重要：**先消费验证码，再判断邮箱是否已注册**。
        # 反过来的话，不掌握该邮箱的人也能通过"是否报邮箱已占用"
        # 批量探测哪些邮箱注册过 —— 等于白送一个账号枚举接口。
        # 先验证码后查询，攻击者必须先能收到邮件才能问出答案。
        await self._verification.consume(
            email=email, scene=VerificationScene.REGISTER.value, code=code
        )

        if await self._users.email_exists(email):
            raise EmailAlreadyRegisteredError()

        user = self._users.add(
            email=email,
            password_hash=hash_password(password),
            nickname=nickname,
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # 并发注册同一邮箱：预检查都通过了，最后由唯一约束拦住。
            # 不能只靠"先查后插"，那中间有窗口，两个请求会同时通过。
            await self._session.rollback()
            raise EmailAlreadyRegisteredError() from exc

        tokens = await self._issue_tokens(user, client_ip=client_ip, user_agent=user_agent)
        await self._session.commit()
        logger.info("user registered id=%s email=%s", user.id, mask_email(email))
        return user, tokens

    # ------------------------------------------------------------------ 登录

    async def login(
        self,
        *,
        email: str,
        password: str,
        client_ip: str | None,
        user_agent: str,
    ) -> tuple[User, IssuedTokens]:
        self._ensure_configured()

        user = await self._users.get_by_email(email)
        if user is None:
            # 跑一次等量的哈希校验，把响应耗时拉平。
            # 不做这一步，登录接口就是个时序预言机：存在=50ms，不存在=1ms。
            verify_password(password, dummy_hash())
            raise InvalidCredentialsError()

        matched, upgraded = verify_password(password, user.password_hash)
        if not matched:
            raise InvalidCredentialsError()

        # 密码对了才检查状态：反过来会泄露"这个账号存在但被停用了"
        if user.status != UserStatus.ACTIVE:
            raise AccountDisabledError()

        if upgraded:
            # 旧算法（bcrypt）验过的密码顺手升级成 argon2id，
            # 用户无感，也不需要单独跑迁移脚本
            user.password_hash = upgraded
            logger.info("password hash upgraded id=%s", user.id)

        self._users.touch_login(user, ip=pack_ip(client_ip))
        tokens = await self._issue_tokens(user, client_ip=client_ip, user_agent=user_agent)
        await self._session.commit()
        logger.info("user login id=%s email=%s", user.id, mask_email(email))
        return user, tokens

    # ------------------------------------------------------------------ 刷新

    async def refresh(
        self,
        *,
        refresh_token: str,
        client_ip: str | None,
        user_agent: str,
    ) -> tuple[User, IssuedTokens]:
        self._ensure_configured()

        record = await self._sessions.get_by_token_hash(hash_refresh_token(refresh_token))
        if record is None:
            raise SessionInvalidError()

        user = await self._users.get_by_id(record.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise SessionInvalidError()

        # 轮换：旧 refresh token 立刻作废，换一对新的。
        # 这样即使 refresh token 被复制走，真用户下次刷新时旧的就失效了，
        # 攻击者拿到的那份也用不了 —— 比"一直用同一个"安全得多。
        self._sessions.revoke(record)
        tokens = await self._issue_tokens(user, client_ip=client_ip, user_agent=user_agent)
        await self._session.commit()
        return user, tokens

    # ------------------------------------------------------------------ 登出

    async def logout(self, *, refresh_token: str) -> None:
        """撤销 refresh token。

        幂等：token 不存在 / 已撤销都当成功返回。
        报错的话，攻击者可以用它判断某个 refresh token 是否有效。
        注意 access token 是自包含的，登出后**在剩余有效期内仍然可用**，
        这是 JWT 的固有代价，换取的是每请求不查库。有效期 15 分钟。
        """
        record = await self._sessions.get_by_token_hash(hash_refresh_token(refresh_token))
        if record is None:
            return
        self._sessions.revoke(record)
        await self._session.commit()

    # ------------------------------------------------------------------ 内部

    def _ensure_configured(self) -> None:
        if not self._settings.jwt_configured:
            raise AuthNotConfiguredError("JWT_SECRET")

    async def _issue_tokens(
        self, user: User, *, client_ip: str | None, user_agent: str
    ) -> IssuedTokens:
        refresh_token = generate_refresh_token()
        record = self._sessions.add(
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=utcnow() + timedelta(seconds=self._settings.REFRESH_TOKEN_TTL_SECONDS),
            ip=pack_ip(client_ip),
            user_agent=user_agent,
        )
        # 必须先 flush 拿到会话主键，access token 里要带 sid
        await self._session.flush()

        access_token, expires_in = create_access_token(user.id, record.id)
        return IssuedTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
        )
