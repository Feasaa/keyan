"""登录失败限流：按账号 + 按 IP 双计数。

为什么必须挡在哈希之前
----------------------
argon2id 单次校验约 50ms CPU。没有失败计数时，`/auth/login` 就是一个
**免认证的 CPU 放大器**：脚本拿错密码狂刷，就能把工作进程占满。
所以计数检查必须在 `verify_password` **之前** —— 挡在哈希之后等于没挡，
CPU 已经花掉了，限流只剩下"事后告知"的作用。

为什么失败也要给"不存在的账号"计数
----------------------------------
只对存在的账号计数，会让"第 N 次返回 429"变成账号存在性预言机：
不存在的邮箱永远 401，存在的邮箱第 6 次变 429，一测就知道。
所以无论账号是否存在，失败一律计数，两者行为完全一致。

为什么成功只清账号计数、不清 IP 计数
------------------------------------
清了 IP 计数的话，攻击者只要掌握**任意一个**有效账号，就能靠它反复
把自己的 IP 计数刷回零，IP 维度直接失效。账号计数是"自己的"，
成功登录当然该清；IP 计数是"这台机器的"，得等窗口自然过期。

已知代价
--------
按账号锁定可以被用来 DoS 某个已知邮箱（连错 5 次锁 15 分钟）。
这是所有 account lockout 方案的共同代价，缓解手段是：
窗口短（默认 15 分钟）、同时按 IP 计数、只影响登录接口不影响其他接口。
"""

import logging

from redis.asyncio import Redis

from app.core.config import Settings
from app.services.exceptions import AuthRateLimitedError

logger = logging.getLogger(__name__)


class LoginThrottle:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ------------------------------------------------------------------ 校验

    async def ensure_allowed(self, *, email: str, client_ip: str | None) -> None:
        """在验密之前拦截。超限直接 429，不消耗任何 CPU。"""
        email_key = self._email_key(email)
        keys = [email_key]
        if client_ip:
            keys.append(self._ip_key(client_ip))

        # 一次 MGET 拿全：两个 GET 会让每个登录请求多一个 RTT
        counts = await self._redis.mget(keys)

        if int(counts[0] or 0) >= self._settings.LOGIN_MAX_ATTEMPTS_PER_EMAIL:
            await self._raise_limited(email_key, "该账号登录失败次数过多")
        if client_ip and int(counts[1] or 0) >= self._settings.LOGIN_MAX_ATTEMPTS_PER_IP:
            await self._raise_limited(self._ip_key(client_ip), "当前网络登录失败次数过多")

    # ------------------------------------------------------------------ 计数

    async def record_failure(self, *, email: str, client_ip: str | None) -> None:
        """记录一次失败。账号不存在时也要调 —— 理由见模块 docstring。"""
        keys = [self._email_key(email)]
        if client_ip:
            keys.append(self._ip_key(client_ip))

        window = self._settings.LOGIN_FAILURE_WINDOW_SECONDS
        # INCR 与 EXPIRE 放进同一个 MULTI/EXEC：分开执行时若进程在两条命令
        # 之间退出，会留下一个**永不过期**的计数器，这个账号就被永久锁死了，
        # 且没有任何日志能看出来。nx=True 保证只在第一次设置过期时间。
        async with self._redis.pipeline(transaction=True) as pipe:
            for key in keys:
                pipe.incr(key)
                pipe.expire(key, window, nx=True)
            await pipe.execute()

    async def reset(self, *, email: str) -> None:
        """登录成功后清掉该账号的失败计数（IP 计数不清，理由见模块 docstring）。"""
        await self._redis.delete(self._email_key(email))

    # ------------------------------------------------------------------ 内部

    async def _raise_limited(self, key: str, prefix: str) -> None:
        remaining = await self._redis.ttl(key)
        # ttl 可能是 -1（无过期）或 -2（不存在）。前者说明计数器出了问题，
        # 宁可让它按窗口算，也不要报一个 "-1 秒后重试" 这种废话。
        window = self._settings.LOGIN_FAILURE_WINDOW_SECONDS
        wait = remaining if remaining and remaining > 0 else window
        minutes = max(1, wait // 60)
        logger.warning("登录被限流 key=%s remaining=%ss", key, wait)
        raise AuthRateLimitedError(f"{prefix}，请 {minutes} 分钟后再试")

    @staticmethod
    def _email_key(email: str) -> str:
        return f"login:fail:email:{email}"

    @staticmethod
    def _ip_key(client_ip: str) -> str:
        return f"login:fail:ip:{client_ip}"
