"""密码哈希与访问令牌的单元测试。

不碰网络也不碰数据库，只验证 `app/core/security.py` 的契约。
配置用 monkeypatch 注入固定值，不依赖开发机的 .env ——
否则换一台机器跑测试，结果就变了。
"""

import pytest
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import (
    create_access_token,
    decode_access_token,
    dummy_hash,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_matches,
    verify_password,
)
from pwdlib.hashers.bcrypt import BcryptHasher

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def secured_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """把 security 模块里的 get_settings 换成固定配置。

    security.py 是 `from app.core.config import get_settings` 之后在函数内部调用的，
    所以 patch 模块属性就能生效 —— 这也是"不要在模块顶层把配置取成常量"的
    好处：那种写法会让配置无法被测试替换。
    """
    base = get_settings()
    patched = base.model_copy(
        update={
            "JWT_SECRET": "unit-test-secret-" + "x" * 32,
            "ACCESS_TOKEN_TTL_SECONDS": 900,
        }
    )
    monkeypatch.setattr("app.core.security.get_settings", lambda: patched)
    return patched


# ------------------------------------------------------------------ 密码


def test_hash_is_argon2id() -> None:
    """新密码必须用 argon2id。

    锁这条是因为换成 md5/sha1（甚至裸 bcrypt）不会报错、测试也照样过，
    只有"看前缀"能拦住。
    """
    hashed = hash_password(PASSWORD)
    assert hashed.startswith("$argon2id$")
    # 255 宽度是表里的上限，超了会被 MySQL 截断，之后再也验不过
    assert len(hashed) < 255


def test_hash_is_salted() -> None:
    """同一密码两次哈希必须不同 —— 否则等于没加盐，彩虹表直接命中。"""
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_verify_roundtrip() -> None:
    hashed = hash_password(PASSWORD)
    assert verify_password(PASSWORD, hashed) == (True, None)


def test_verify_rejects_wrong_password() -> None:
    hashed = hash_password(PASSWORD)
    matched, upgraded = verify_password(PASSWORD + "x", hashed)
    assert matched is False
    assert upgraded is None


def test_verify_survives_corrupted_hash() -> None:
    """密文损坏当校验失败，不能抛异常。

    抛出去就是 500，而 500 和 401 的区别本身就泄露了
    "这个账号的密文有问题"，对攻击者是有用信息。
    """
    assert verify_password(PASSWORD, "not-a-hash") == (False, None)
    assert verify_password(PASSWORD, "") == (False, None)


def test_bcrypt_hash_is_upgraded_to_argon2id() -> None:
    """旧 bcrypt 密文能验过，并且顺手给出新的 argon2id 密文。

    这是"换算法不用强制所有人改密"的关键：登录一次就自动升级。
    """
    legacy = BcryptHasher().hash(PASSWORD)
    assert legacy.startswith("$2")

    matched, upgraded = verify_password(PASSWORD, legacy)
    assert matched is True
    assert upgraded is not None
    assert upgraded.startswith("$argon2id$")
    # 新密文必须能验过，否则"升级"等于把用户锁在门外
    assert verify_password(PASSWORD, upgraded)[0] is True


def test_dummy_hash_is_stable_and_verifiable() -> None:
    """占位哈希只算一次，且必须是一个合法密文。

    登录时对不存在的用户跑它来拉平耗时；如果它本身不合法，
    verify 会立刻返回，耗时拉不平，等于白做。
    """
    first = dummy_hash()
    assert dummy_hash() is first
    assert verify_password("anything", first) == (False, None)


# ------------------------------------------------------------------ 令牌


def test_access_token_roundtrip(secured_settings: Settings) -> None:
    token, expires_in = create_access_token(user_id=123456789012345678, session_id=987)
    assert expires_in == secured_settings.ACCESS_TOKEN_TTL_SECONDS

    claims = decode_access_token(token)
    assert claims.user_id == 123456789012345678
    assert claims.session_id == 987
    assert claims.jti


def test_access_token_uses_string_sub(secured_settings: Settings) -> None:
    """sub 必须是字符串。

    JWT 规范要求 sub 为 string；PyJWT 2.10+ 会直接拒绝整数 sub。
    而雪花 ID 是 19 位，前端 JS 解析 JSON 时会丢精度 ——
    所以对外也一律给字符串，不只是 token 内部。
    """
    import jwt

    token, _ = create_access_token(user_id=123456789012345678, session_id=1)
    payload = jwt.decode(token, options={"verify_signature": False})
    assert isinstance(payload["sub"], str)
    assert payload["sub"] == "123456789012345678"


def test_token_signed_with_other_secret_is_rejected(secured_settings: Settings) -> None:
    token, _ = create_access_token(user_id=1, session_id=1)
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(AppError):
        decode_access_token(tampered)


def test_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 TTL 设成负数来造一个过期 token，不用真的等 15 分钟。"""
    base = get_settings()
    patched = base.model_copy(
        update={"JWT_SECRET": "unit-test-secret-" + "x" * 32, "ACCESS_TOKEN_TTL_SECONDS": -1}
    )
    monkeypatch.setattr("app.core.security.get_settings", lambda: patched)

    token, _ = create_access_token(user_id=1, session_id=1)
    with pytest.raises(AppError) as excinfo:
        decode_access_token(token)
    assert "过期" in str(excinfo.value)


def test_alg_none_token_is_rejected(secured_settings: Settings) -> None:
    """alg=none 是 JWT 最经典的绕过手法。

    攻击者把 header 的 alg 改成 none、签名段留空，实现如果"按 token 自己
    声明的算法去验"，就会直接跳过签名校验。所以 decode 时必须显式给
    algorithms 白名单。
    """
    import base64
    import json

    def b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64({"alg": "none", "typ": "JWT"})
    body = b64({"sub": "1", "sid": "1", "typ": "access", "exp": 9999999999})
    forged = f"{header}.{body}."
    with pytest.raises(AppError):
        decode_access_token(forged)


def test_wrong_token_type_is_rejected(secured_settings: Settings) -> None:
    """typ 不是 access 的 token 不能当 access 用。

    现在只签 access，但将来若加"重置密码令牌""邮箱验证令牌"，
    它们都是同一把密钥签的 —— 不校验 typ 就可以拿低权限令牌换高权限操作。
    """
    import jwt

    payload = {
        "sub": "1",
        "sid": "1",
        "typ": "reset_password",
        "exp": 9999999999,
        "iat": 1,
        "jti": "x",
    }
    token = jwt.encode(payload, secured_settings.JWT_SECRET, algorithm="HS256")
    with pytest.raises(AppError):
        decode_access_token(token)


# ------------------------------------------------------------------ refresh token


def test_refresh_token_is_high_entropy() -> None:
    """1000 个令牌不能有重复，长度也不能太短。

    用 uuid4 会短一截且格式可预测；这条测试锁住"随机源够不够"。
    """
    tokens = {generate_refresh_token() for _ in range(1000)}
    assert len(tokens) == 1000
    assert all(len(token) >= 43 for token in tokens)


def test_refresh_token_hash_is_deterministic() -> None:
    token = generate_refresh_token()
    assert hash_refresh_token(token) == hash_refresh_token(token)
    # sha256 十六进制恒为 64 字符，对应表里的 CHAR(64)
    assert len(hash_refresh_token(token)) == 64


def test_refresh_token_match() -> None:
    token = generate_refresh_token()
    stored = hash_refresh_token(token)
    assert refresh_token_matches(token, stored) is True
    assert refresh_token_matches(generate_refresh_token(), stored) is False
