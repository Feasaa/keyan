"""密码哈希与访问令牌。

密码
----
argon2id 是新密码的唯一算法，bcrypt 只保留**校验**能力，用于历史密文平滑迁移。
模型里 password_hash 留了 255 宽度就是为了这个：换算法不用改表，
按密文前缀（`$argon2id$` / `$2b$`）分流即可。

为什么不用 passlib：
    passlib 1.7.4 依赖标准库 `crypt` 模块，而它在 Python 3.13 被移除，
    import 阶段就炸；且 1.7.4 读 bcrypt 的 `__about__.__version__`，
    与 bcrypt 4.x 不兼容。pwdlib 是它的替代品，API 更小，
    自带 verify_and_update 做"校验旧算法 + 顺手换成新算法"。

为什么不自己拼 bcrypt：
    密码学库的坑集中在"参数"上 —— 迭代轮数、salt 长度、并行度。
    自己拼很容易写出"能跑但强度不够"的配置，而且不会报错。

令牌
----
access 是 JWT（自包含、不落库、15 分钟），refresh 是随机串（落库、30 天、可撤销）。
这个组合的理由：
    - access 每请求都要验，不能查库，所以必须自包含；
    - 自包含的代价是**无法提前失效**，所以有效期必须短；
    - 要"立刻踢人"就必须有可撤销的凭证，那就是 refresh —— 它只在换票时用一次，
      查一次库的成本可以接受。
    纯 JWT 方案的软肋就是"登出后旧 token 还能用"，纯 session 方案的软肋是
    "每请求一次 Redis/DB"。分开用各取所长。
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, UnauthorizedError

# 顺序有意义：hash 永远用第一个（argon2id），verify 会按密文前缀自动选
_PASSWORD_HASH = PasswordHash((Argon2Hasher(), BcryptHasher()))

TOKEN_TYPE_ACCESS = "access"


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: int
    session_id: int
    jti: str
    expires_at: datetime


# ------------------------------------------------------------------ 密码


def hash_password(raw: str) -> str:
    """生成 argon2id 密文。每次调用 salt 都不同，同一密码的密文也不同。"""
    return _PASSWORD_HASH.hash(raw)


def verify_password(raw: str, hashed: str) -> tuple[bool, str | None]:
    """校验密码，返回 (是否通过, 需要写回的新密文)。

    第二个返回值非空表示"这次是用旧算法验过的"，调用方应把它写回库 ——
    用户下次登录就自动升级到 argon2id，不需要强制改密，也不需要批量迁移脚本。
    """
    try:
        return _PASSWORD_HASH.verify_and_update(raw, hashed)
    except Exception:
        # 密文损坏 / 算法不认识：当作校验失败，不要把异常抛给调用方。
        # 抛出去会变成 500，等于告诉攻击者"这个账号的密文有问题"。
        return False, None


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """一个恒定存在的合法密文，用于"用户不存在"时消耗等量 CPU。

    不这么做的话，登录接口会变成一个**时序预言机**：
    邮箱存在 → 跑一次 argon2（约 50ms）；邮箱不存在 → 立刻返回（约 1ms）。
    攻击者不用看响应内容，只看耗时就能批量判断哪些邮箱注册过。
    """
    return hash_password(secrets.token_urlsafe(32))


# ------------------------------------------------------------------ 令牌


def create_access_token(user_id: int, session_id: int) -> tuple[str, int]:
    """签发 access token，返回 (token, 有效期秒数)。"""
    settings = get_settings()
    if not settings.jwt_configured:
        raise AppError(ErrorCode.AUTH_NOT_CONFIGURED, "鉴权未配置：请设置 JWT_SECRET")

    ttl = settings.ACCESS_TOKEN_TTL_SECONDS
    now = datetime.now(UTC)
    payload = {
        # JWT 规范要求 sub 是字符串。雪花 ID 是 19 位整数，直接放进去
        # PyJWT 2.10+ 会直接报错；而且 JS 侧解析会丢精度。
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": TOKEN_TYPE_ACCESS,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, ttl


def decode_access_token(token: str) -> AccessTokenClaims:
    """校验并解出 claims。任何问题都收敛成 UnauthorizedError。

    对外只给"未登录或登录已失效"一句话：token 过期、签名不对、算法被改，
    对客户端来说处理方式完全一样（重新登录），区分只会泄露实现细节。
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            # 必须显式指定算法白名单。不指定的话存在 alg=none 与
            # HS/RS 混用攻击：攻击者把算法改成 none，签名就直接不校验了。
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError() from exc

    if payload.get("typ") != TOKEN_TYPE_ACCESS:
        raise UnauthorizedError()

    try:
        return AccessTokenClaims(
            user_id=int(payload["sub"]),
            session_id=int(payload["sid"]),
            jti=str(payload.get("jti", "")),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthorizedError() from exc


def generate_refresh_token() -> str:
    """32 字节随机串，urlsafe 编码后 43 字符。

    用 secrets 而不是 uuid4：uuid4 只有 122 bit 随机性且格式可预测，
    token 这类"猜中即登录"的凭证必须用密码学安全的随机源。
    """
    return secrets.token_urlsafe(32)


def hash_refresh_token(raw: str) -> str:
    """落库前哈希。

    为什么是 sha256 而不是 argon2：
        refresh token 本身就是 256 bit 的高熵随机串，不存在"弱口令"问题，
        不需要慢哈希抗暴力；而它每次换票都要查库，
        用 argon2 会让登录/刷新接口的延迟凭空多几十毫秒。
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_token_matches(raw: str, stored_hash: str) -> bool:
    """常量时间比较，避免通过响应耗时逐字节猜出哈希。"""
    return hmac.compare_digest(hash_refresh_token(raw), stored_hash)
