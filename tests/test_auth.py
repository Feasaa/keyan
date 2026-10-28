"""鉴权接口的端到端测试（真 MySQL + 真 Redis db 15 + 桩邮件）。

数据库/Redis 没起时整组 skip —— 注册要真写库，用假的测不出唯一约束、
事务边界、级联删除这些真正会出事的地方。
"""

import time

from fastapi.testclient import TestClient

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
