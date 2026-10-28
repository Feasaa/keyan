"""鉴权接口的端到端测试（真 MySQL + 真 Redis db 15 + 桩邮件）。

数据库/Redis 没起时整组 skip —— 注册要真写库，用假的测不出唯一约束、
事务边界、级联删除这些真正会出事的地方。
"""

import asyncio
import logging
import threading
import time
from datetime import timedelta

from app.core.config import get_settings
from app.core.security import hash_refresh_token
from app.db.base import utcnow
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

CODE_PATH = "/api/v1/auth/verification-code"
REGISTER_PATH = "/api/v1/auth/register"
LOGIN_PATH = "/api/v1/auth/login"
REFRESH_PATH = "/api/v1/auth/refresh"
LOGOUT_PATH = "/api/v1/auth/logout"
ME_PATH = "/api/v1/auth/me"

PASSWORD = "correct horse battery staple"


def _send_code(client: TestClient, email: str) -> None:
    response = client.post(CODE_PATH, json={"email": email})
    assert response.status_code == 200, response.text


def _register(client: TestClient, mail, email: str, password: str = PASSWORD):
    _send_code(client, email)
    return client.post(
        REGISTER_PATH,
        json={"email": email, "password": password, "code": mail.last_code},
    )


def _register_ok(client: TestClient, mail, email: str) -> dict:
    response = _register(client, mail, email)
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post(LOGIN_PATH, json={"email": email, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _age_revoked_at(refresh_token: str, seconds: int) -> None:
    """把某条会话的撤销时间往前拨，用来跨越 refresh 复用宽限期。

    为什么改数据而不是 `time.sleep(31)`：
        "宽限期"只是 `_report_reuse` 里的一次时间减法，等真实时间既让用例
        慢 30 秒，又把结果绑在机器负载上（调度延迟会让边界变得不确定）。
        改 `revoked_at` 是把"30 秒过去了"这个前提直接摆好。

    注意这里复用应用自己的 `utcnow()`：全链路约定 naive UTC，
    用 `NOW()` 之类的库端时间函数会引入时区假设，一旦库的 time_zone
    不是 UTC，`_report_reuse` 算出来的 age 就是负的，用例会以
    "宽限期意外生效"的形式失败，且极难定位。
    """
    settings = get_settings()
    token_hash = hash_refresh_token(refresh_token)

    async def run() -> None:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    text("UPDATE auth_sessions SET revoked_at = :ts WHERE refresh_token_hash = :h"),
                    {"ts": utcnow() - timedelta(seconds=seconds), "h": token_hash},
                )
                assert result.rowcount == 1, "没有匹配到会话，用例前提不成立"
        finally:
            await engine.dispose()

    asyncio.run(run())


# ------------------------------------------------------------------ 注册


def test_register_returns_tokens(auth_client: TestClient, stub_mail, make_email) -> None:
    """注册成功直接给令牌，省掉"注册完再登一次"的往返。"""
    email = make_email()
    body = _register_ok(auth_client, stub_mail, email)

    assert body["code"] == 0
    assert body["trace_id"]
    data = body["data"]
    assert data["user"]["email"] == email
    assert data["user"]["nickname"] == email.split("@", 1)[0]
    assert data["token"]["token_type"] == "Bearer"
    assert data["token"]["expires_in"] > 0
    assert data["token"]["access_token"]
    assert data["token"]["refresh_token"]


def test_user_id_is_string(auth_client: TestClient, stub_mail, make_email) -> None:
    """雪花 ID 必须以字符串返回。

    19 位整数超出 JS 的 Number.MAX_SAFE_INTEGER（2^53-1），
    前端解析后末几位会被静默改写 —— 表现为"用户 ID 对不上"，
    而且前端不会报任何错。这条测试锁住类型。
    """
    body = _register_ok(auth_client, stub_mail, make_email())
    user_id = body["data"]["user"]["id"]
    assert isinstance(user_id, str)
    assert user_id.isdigit()
    assert len(user_id) >= 18


def test_created_at_has_timezone(auth_client: TestClient, stub_mail, make_email) -> None:
    """时间必须带时区。

    库里存的是 naive UTC，直接吐出去前端 new Date() 会按本地时区解析，
    东八区直接错 8 小时。
    """
    body = _register_ok(auth_client, stub_mail, make_email())
    created_at = body["data"]["user"]["created_at"]
    assert created_at.endswith("Z") or "+00:00" in created_at


def test_register_duplicate_email(auth_client: TestClient, stub_mail, make_email) -> None:
    """同一邮箱第二次注册返回 409。

    注意这里要先等冷却过去再发第二个码 —— 验证码是"先消费再查重"的，
    没有码就查不了重（否则接口变成账号枚举工具）。
    """
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    time.sleep(1.2)  # 测试配置把冷却缩到 1 秒
    response = _register(auth_client, stub_mail, email)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == 11409


def test_register_wrong_code(auth_client: TestClient, stub_mail, make_email) -> None:
    email = make_email()
    _send_code(auth_client, email)
    wrong = "000000" if stub_mail.last_code != "000000" else "111111"

    response = auth_client.post(
        REGISTER_PATH, json={"email": email, "password": PASSWORD, "code": wrong}
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == 11400


def test_register_rejects_short_password(auth_client: TestClient, make_email) -> None:
    """密码 7 位要被参数校验拦下（422，不是 400）。"""
    response = auth_client.post(
        REGISTER_PATH,
        json={"email": make_email(), "password": "1234567", "code": "123456"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == 10422


def test_register_rejects_bad_code_format(auth_client: TestClient, make_email) -> None:
    response = auth_client.post(
        REGISTER_PATH,
        json={"email": make_email(), "password": PASSWORD, "code": "abc"},
    )
    assert response.status_code == 422


def test_register_rejects_invalid_email(auth_client: TestClient) -> None:
    response = auth_client.post(CODE_PATH, json={"email": "not-an-email"})
    assert response.status_code == 422


# ------------------------------------------------------------------ 登录


def test_login_success(auth_client: TestClient, stub_mail, make_email) -> None:
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    response = _login(auth_client, email)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["user"]["email"] == email


def test_login_wrong_password(auth_client: TestClient, stub_mail, make_email) -> None:
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    response = _login(auth_client, email, "definitely-not-the-password")
    assert response.status_code == 401
    assert response.json()["code"] == 11401


def test_login_unknown_email_looks_identical(
    auth_client: TestClient, stub_mail, make_email
) -> None:
    """密码错和邮箱不存在必须返回**完全一样**的响应。

    只要有一处不同（状态码、错误码、文案、甚至耗时），
    登录接口就变成了账号枚举工具。这里连 message 都比对。
    """
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    wrong_password = _login(auth_client, email, "definitely-not-the-password")
    unknown_email = _login(auth_client, make_email())

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["code"] == unknown_email.json()["code"] == 11401
    assert wrong_password.json()["message"] == unknown_email.json()["message"]


def test_login_email_is_case_insensitive(auth_client: TestClient, stub_mail, make_email) -> None:
    """注册用小写、登录用大写也能进 —— 邮箱统一在入口处 lower()。"""
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    response = _login(auth_client, email.upper())
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------ 登录失败限流

MAX_ATTEMPTS = 5


def test_login_locks_account_after_repeated_failures(
    auth_client: TestClient, stub_mail, make_email
) -> None:
    """连错 5 次后锁定，且**密码正确也进不去**。

    没有这道闸时，登录接口就是个免认证的 CPU 放大器：
    argon2 每次约 50ms，脚本狂刷就能把工作进程占满。
    """
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    for _ in range(MAX_ATTEMPTS):
        assert _login(auth_client, email, "wrong password").status_code == 401

    blocked = _login(auth_client, email, "wrong password")
    assert blocked.status_code == 429
    body = blocked.json()
    assert body["code"] == 11429
    assert "分钟" in body["message"]

    # 关键：锁定期间连正确密码也拒绝，否则限流等于没有
    assert _login(auth_client, email, PASSWORD).status_code == 429


def test_login_lockout_does_not_reveal_account_existence(
    auth_client: TestClient, make_email
) -> None:
    """不存在的邮箱同样会被锁成 429。

    只锁"存在的账号"的话，第 6 次请求返回 401 还是 429
    就成了账号存在性预言机 —— 而账号枚举正是登录接口最该防的事。
    """
    ghost = make_email()

    for _ in range(MAX_ATTEMPTS):
        assert _login(auth_client, ghost, "whatever").status_code == 401

    assert _login(auth_client, ghost, "whatever").status_code == 429


def test_login_success_resets_failure_counter(
    auth_client: TestClient, stub_mail, make_email
) -> None:
    """成功登录清掉本账号计数，别让用户"错几次之后一直正常"也被锁。"""
    email = make_email()
    _register_ok(auth_client, stub_mail, email)

    for _ in range(MAX_ATTEMPTS - 1):
        assert _login(auth_client, email, "wrong password").status_code == 401

    assert _login(auth_client, email, PASSWORD).status_code == 200

    # 计数已清零：再错一次应该是 401，而不是撞到第 6 次被锁
    assert _login(auth_client, email, "wrong password").status_code == 401


# ------------------------------------------------------------------ 令牌


def test_me_returns_profile(auth_client: TestClient, stub_mail, make_email) -> None:
    email = make_email()
    body = _register_ok(auth_client, stub_mail, email)
    token = body["data"]["token"]["access_token"]

    response = auth_client.get(ME_PATH, headers=_bearer(token))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["email"] == email


def test_me_without_token_uses_envelope(auth_client: TestClient) -> None:
    """缺令牌也必须是统一外壳 + 401。

    HTTPBearer 默认 auto_error=True 时会自己抛 403 和 {"detail": ...}，
    全站唯一的响应格式就此破功 —— 所以这里用 auto_error=False。
    """
    response = auth_client.get(ME_PATH)
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == 11401
    assert "trace_id" in body


def test_me_with_garbage_token(auth_client: TestClient) -> None:
    response = auth_client.get(ME_PATH, headers=_bearer("not-a-jwt"))
    assert response.status_code == 401
    assert response.json()["code"] == 11401


def test_refresh_rotates_token(auth_client: TestClient, stub_mail, make_email) -> None:
    """刷新后旧 refresh token 立刻失效（轮换）。

    不轮换的话，refresh token 一旦被复制走就永久有效，
    而且服务端没有任何信号能发现"同一份凭证在两个地方用"。
    """
    body = _register_ok(auth_client, stub_mail, make_email())
    old_refresh = body["data"]["token"]["refresh_token"]

    refreshed = auth_client.post(REFRESH_PATH, json={"refresh_token": old_refresh})
    assert refreshed.status_code == 200, refreshed.text
    new_refresh = refreshed.json()["data"]["token"]["refresh_token"]
    assert new_refresh != old_refresh

    reused = auth_client.post(REFRESH_PATH, json={"refresh_token": old_refresh})
    assert reused.status_code == 401, reused.text


def test_logout_revokes_refresh_token(auth_client: TestClient, stub_mail, make_email) -> None:
    body = _register_ok(auth_client, stub_mail, make_email())
    refresh_token = body["data"]["token"]["refresh_token"]

    assert auth_client.post(LOGOUT_PATH, json={"refresh_token": refresh_token}).status_code == 200

    after = auth_client.post(REFRESH_PATH, json={"refresh_token": refresh_token})
    assert after.status_code == 401


def test_logout_is_idempotent(auth_client: TestClient) -> None:
    """不存在的 token 也返回成功。

    报错的话，这个接口就成了"判断某个 refresh token 是否有效"的探测工具。
    """
    response = auth_client.post(LOGOUT_PATH, json={"refresh_token": "never-existed"})
    assert response.status_code == 200
    assert response.json()["code"] == 0


def test_refresh_with_garbage_token(auth_client: TestClient) -> None:
    response = auth_client.post(REFRESH_PATH, json={"refresh_token": "nope"})
    assert response.status_code == 401


def _refresh(client: TestClient, token: str):
    return client.post(REFRESH_PATH, json={"refresh_token": token})


def test_concurrent_refresh_issues_only_one_token(
    auth_client: TestClient, stub_mail, make_email
) -> None:
    """同一个 token 并发刷新，只能有一个成功。

    没有 `SELECT ... FOR UPDATE` 时，两个请求各自在自己的快照里看到
    `revoked_at IS NULL`，于是各轮换出一对令牌 —— 一次刷新变出两条有效链路，
    客户端只拿到其中一条，另一条成了没人使用却依然有效的"幽灵登录态"。

    说明：真正重叠时才可能双发，而线程调度不保证重叠；
    因此这个用例的作用是"有重叠就一定抓到"，不是"没重叠也能抓到"。
    屏障让两个线程尽量同时进入请求，提高撞上窗口的概率。
    """
    token = _register_ok(auth_client, stub_mail, make_email())["data"]["token"]["refresh_token"]

    barrier = threading.Barrier(2, timeout=5)
    codes: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        code = _refresh(auth_client, token).status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(codes) == [200, 401], f"期望恰好一次成功，实际 {sorted(codes)}"


def test_replay_beyond_grace_revokes_whole_family(
    auth_client: TestClient, stub_mail, make_email, caplog
) -> None:
    """超过宽限期重放旧 token：整条登录链路作废。

    只撤被重放的那一个没用 —— 攻击者手上可能是链路中**任意一代**的 token。
    既然已经能确认"有人拿着早就轮换掉的凭证在用"，唯一安全的假设是
    这条链路全线泄露，必须连新签发的一起撤掉。
    """
    body = _register_ok(auth_client, stub_mail, make_email())
    old_refresh = body["data"]["token"]["refresh_token"]
    new_refresh = _refresh(auth_client, old_refresh).json()["data"]["token"]["refresh_token"]

    grace = get_settings().REFRESH_REUSE_GRACE_SECONDS
    _age_revoked_at(old_refresh, grace + 60)

    with caplog.at_level(logging.WARNING, logger="app.services.auth_service"):
        replay = _refresh(auth_client, old_refresh)
    assert replay.status_code == 401

    # 关键断言：不是"这个 token 失效"，而是链路作废 —— 刚签发的那份也一起废了
    assert _refresh(auth_client, new_refresh).status_code == 401

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("refresh token 复用" in message for message in warnings), warnings


def test_replay_within_grace_keeps_family(auth_client: TestClient, stub_mail, make_email) -> None:
    """宽限期内重放按"客户端重试"处理，不能一刀切撤销。

    并发刷新、网络超时重试都会在轮换后几秒内再发一次旧 token。
    这里若也撤整条链路，正常用户会被自己的重试踢下线 ——
    安全策略的误伤成本和漏报成本要一起算，30 秒的窗口就是在买这个平衡。
    """
    body = _register_ok(auth_client, stub_mail, make_email())
    old_refresh = body["data"]["token"]["refresh_token"]
    new_refresh = _refresh(auth_client, old_refresh).json()["data"]["token"]["refresh_token"]

    assert _refresh(auth_client, old_refresh).status_code == 401

    # 链路没被撤：新 token 照常可用，而且能继续轮换下去
    again = _refresh(auth_client, new_refresh)
    assert again.status_code == 200, again.text
