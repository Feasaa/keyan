"""邮箱验证码：签发、校验、限流。

为什么自建而不是接第三方
------------------------
验证码的"状态"必须和注册动作在同一个事务语义里 —— 校验通过才能建号。
第三方短信/邮件验证码服务通常只给一个"校验通过"的回调或票据，
跨系统对账会引入"码验过了但建号失败了"这类中间态。既然已经有 Redis，
自建的成本比接第三方低，且完全可控。

三个设计点
----------
1. **码存哈希不存明文**。和密码同理：Redis 常被开成无密码、可远程访问、
   顺手 `KEYS *` 就能 dump。存明文的话，一次配置失误等于把所有在途验证码公开。
   这里用 sha256 而不是 argon2 —— 6 位数字只有 100 万种可能，
   慢哈希也挡不住离线穷举，真正的作用是"不直接可读"，所以用快速哈希。
   防穷举靠的是**在线**的试错次数限制，不是哈希强度。

2. **三层限流**，缺一层都能被打穿：
   - 60 秒冷却：防"同一个邮箱被反复轰炸"，也防用户连点
   - 每邮箱每日上限：防单点轰炸（换个 IP 也没用）
   - 每 IP 每小时上限：防换邮箱刷（一台机器批量注册）

3. **试错次数上限**。6 位数字在 5 分钟有效期内，不限次数的前提下
   脚本可以穷举完 —— 必须在服务端计数，且计数与验证码同寿命。
"""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.config import Settings
from app.services.exceptions import (
    AuthNotConfiguredError,
    AuthRateLimitedError,
    VerificationCodeError,
)
from app.services.mail_service import MailService
from app.utils.masking import mask_email

logger = logging.getLogger(__name__)

# 6 位数字：100 万种组合，配合 5 次试错上限与 5 分钟有效期，
# 单码被猜中的概率上限是 5/1000000，且每次猜都要走一次网络请求。
CODE_LENGTH = 6
CODE_MAX_VALUE = 10**CODE_LENGTH


class VerificationService:
    def __init__(self, redis: Redis, mail: MailService, settings: Settings) -> None:
        self._redis = redis
        self._mail = mail
        self._settings = settings

    # ------------------------------------------------------------------ 签发

    async def issue(self, *, email: str, scene: str, client_ip: str | None) -> tuple[int, int]:
        """签发并发送验证码，返回 (有效期秒数, 可重发倒计时秒数)。"""
        settings = self._settings
        # 先确认邮件功能可用，再动限流计数 ——
        # 反过来会让"SMTP 没配好"这件事白白吃掉用户的每日配额。
        self._ensure_configured()
        cooldown_key = self._key("cd", scene, email)

        remaining = await self._redis.ttl(cooldown_key)
        if remaining and remaining > 0:
            raise AuthRateLimitedError(f"请求过于频繁，请 {remaining} 秒后再试")

        await self._consume_quota(
            key=self._email_quota_key(email),
            limit=settings.VERIFICATION_DAILY_LIMIT_PER_EMAIL,
            window_seconds=24 * 3600,
            message="该邮箱今日获取验证码次数已达上限，请明天再试",
        )
        if client_ip:
            await self._consume_quota(
                key=self._ip_quota_key(client_ip),
                limit=settings.VERIFICATION_HOURLY_LIMIT_PER_IP,
                window_seconds=3600,
                message="当前网络获取验证码过于频繁，请稍后再试",
            )

        code = self._generate_code()
        ttl = settings.VERIFICATION_CODE_TTL_SECONDS
        code_key = self._key("code", scene, email)
        attempts_key = self._key("attempts", scene, email)

        # 新码覆盖旧码，同时把试错计数清零 —— 否则用户重发一次就把
        # 上一轮用掉的尝试次数带过来，第二次重发后直接不可用。
        await self._redis.set(code_key, self._digest(scene, email, code), ex=ttl)
        await self._redis.delete(attempts_key)
        await self._redis.set(cooldown_key, "1", ex=settings.VERIFICATION_CODE_RESEND_SECONDS)

        try:
            await self._mail.send_verification_code(
                to=email, code=code, scene=scene, ttl_seconds=ttl
            )
        except Exception:
            # 发信失败：把码和冷却一起清掉，让用户能立刻重试，
            # 而不是被自己的 60 秒冷却挡在门外干等。
            await self._redis.delete(code_key, cooldown_key)
            raise

        logger.info("verification code issued to=%s scene=%s", mask_email(email), scene)
        return ttl, settings.VERIFICATION_CODE_RESEND_SECONDS

    # ------------------------------------------------------------------ 校验

    async def consume(self, *, email: str, scene: str, code: str) -> None:
        """校验并消费验证码。成功即作废（一次性使用）。"""
        settings = self._settings
        code_key = self._key("code", scene, email)
        attempts_key = self._key("attempts", scene, email)

        stored = await self._redis.get(code_key)
        if not stored:
            raise VerificationCodeError("验证码已过期，请重新获取")

        # INCR 是原子的，所以并发暴力破解也会被这个计数挡住 ——
        # 1000 个并发请求拿到的计数是 1..1000，只有前 5 个能走到比对。
        attempts = await self._redis.incr(attempts_key)
        if attempts == 1:
            remaining_ttl = await self._redis.ttl(code_key)
            await self._redis.expire(attempts_key, max(remaining_ttl, 1))

        if attempts > settings.VERIFICATION_CODE_MAX_ATTEMPTS:
            # 超限直接作废，不给"继续试到对为止"的机会
            await self._redis.delete(code_key, attempts_key)
            logger.warning("verification code exhausted to=%s scene=%s", mask_email(email), scene)
            raise VerificationCodeError("验证码错误次数过多，请重新获取")

        if not hmac.compare_digest(stored, self._digest(scene, email, code)):
            raise VerificationCodeError()

        # 消费成功。这里"比对"和"删除"不是原子的：
        # 极端并发下同一个正确码可能被消费两次。风险可接受 ——
        # 计数上限已经保证同时最多 5 个请求能走到这里，而注册还有
        # uk_users_email 兜底，重复建号会被数据库拒绝。
        await self._redis.delete(code_key, attempts_key)
        logger.info("verification code consumed to=%s scene=%s", mask_email(email), scene)

    # ------------------------------------------------------------------ 内部

    def _ensure_configured(self) -> None:
        if not self._settings.mail_configured:
            raise AuthNotConfiguredError("SMTP_USER / SMTP_PASSWORD")

    @staticmethod
    def _generate_code() -> str:
        """密码学安全随机。

        绝不能用 random.randint：Mersenne Twister 的输出序列
        在观测到足够多样本后可被完全预测，验证码场景下等于没有随机性。
        """
        return f"{secrets.randbelow(CODE_MAX_VALUE):0{CODE_LENGTH}d}"

    @staticmethod
    def _digest(scene: str, email: str, code: str) -> str:
        """把场景和邮箱一起拌进哈希。

        key 里已经有 email/scene 了，这里再拌一次是防御性的：
        万一哪天有人改了 key 的拼法导致串场景，哈希仍然对不上。
        """
        raw = f"{scene}:{email}:{code}".encode()
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _key(kind: str, scene: str, email: str) -> str:
        return f"vc:{kind}:{scene}:{email}"

    @staticmethod
    def _email_quota_key(email: str) -> str:
        day = datetime.now(UTC).strftime("%Y%m%d")
        return f"vc:quota:email:{email}:{day}"

    @staticmethod
    def _ip_quota_key(client_ip: str) -> str:
        hour = datetime.now(UTC).strftime("%Y%m%d%H")
        return f"vc:quota:ip:{client_ip}:{hour}"

    async def _consume_quota(
        self, *, key: str, limit: int, window_seconds: int, message: str
    ) -> None:
        """计数 +1，超限抛异常。

        INCR 与 EXPIRE 放在同一个 MULTI/EXEC 里：分开执行时，
        如果进程恰好在两条命令之间退出，会留下一个**永不过期**的计数器，
        这个用户（或这个 IP）就被永久封死了，且没有任何日志能看出来。
        `nx=True` 保证只在第一次设置过期时间，不会每来一次就续期。
        """
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds, nx=True)
            count, _ = await pipe.execute()

        if int(count) > limit:
            raise AuthRateLimitedError(message)
