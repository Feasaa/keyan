"""VAPTCHA V4 人机验证：服务端二次验签。

锁住这些契约：
- 未配置 VID/VKey = 功能关闭，没带票据也放行（本地开发不申请也能跑）
- 配置了但票据缺失 / 验签不通过 = 400 / 11400（与邮箱验证码同码，文案区分）
- 上游超时 / 非 200 / 调用失败 = fail-closed，504 / 21504，绝不放行
- remote 模式按官方 data.result + data.code 判定；code=1 默认拒绝，开关打开才放行
- local 模式按官方 HMAC-SHA256 算法校验，并对票据做一次性标记（防重放）

上游用 httpx.MockTransport 打桩，Redis 用内存假客户端，不起真实网络。
异步用例与 test_verification.py 同风格：asyncio.run 包场景，不依赖 anyio 插件。
"""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import pytest
from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.auth import CaptchaPayload
from app.services.exceptions import CaptchaInvalidError, CaptchaUpstreamError
from app.services.vaptcha_service import VaptchaService
from fastapi.testclient import TestClient

VID = "id_test_vid"
VKEY = "key_test_vkey"
CAPTCHA = CaptchaPayload(token="tok", knock="kno", dfu="", ip="")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"VAPTCHA_VID": VID, "VAPTCHA_VKEY": VKEY}
    base.update(overrides)
    return get_settings().model_copy(update=base)


class FakeRedis:
    """只实现 `set(nx=True)`：够验"票据只能用一次"这条契约。"""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True


def _remote(
    payload: dict[str, Any],
    status: int = 200,
    *,
    configured: bool = True,
) -> VaptchaService:
    """remote 模式。status=0 模拟网络不可达。"""

    def handle(request: httpx.Request) -> httpx.Response:
        if status == 0:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(status, json=payload)

    if not configured:
        return VaptchaService(_settings(VAPTCHA_VID="", VAPTCHA_VKEY=""))
    return VaptchaService(_settings(), transport=httpx.MockTransport(handle))


def _ok(**data: Any) -> dict[str, Any]:
    """官方成功响应外壳，data 里默认给"通过"态。"""
    fields: dict[str, Any] = {"code": 0, "note": "success", "result": True, "vid": VID}
    fields.update(data)
    return {"code": 0, "msg": "success", "data": fields}


def _local() -> VaptchaService:
    return VaptchaService(_settings(VAPTCHA_VERIFY_MODE="local"), redis=FakeRedis())


def _signed_token(*, ip: str = "", dfu: str = "", knock: str = "kno", age: int = 0) -> str:
    """按官方算法签一张票据：HMAC-SHA256(VKEY, f"{ts}.{ip}.{dfu}.{knock}")。"""
    ts = str(int(time.time()) - age)
    data = f"{ts}.{ip}.{dfu}.{knock}"
    signature = hmac.new(VKEY.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.tokenid.{signature}"


def _verify(
    service: VaptchaService, captcha: CaptchaPayload = CAPTCHA, ip: str = "1.2.3.4"
) -> None:
    asyncio.run(service.verify(captcha, client_ip=ip))


# ------------------------------------------------------------------ 开关


def test_disabled_passes_without_captcha() -> None:
    """未配置 = 关闭：票据为空也直接放行，不发网络请求。"""
    _verify(_remote({}, configured=False), CaptchaPayload())


def test_enabled_missing_token_rejected() -> None:
    """配置了验签却没带票据：直接拒绝，不浪费上游往返。"""
    with pytest.raises(CaptchaInvalidError):
        _verify(_remote(_ok()), CaptchaPayload())


# ------------------------------------------------------------------ remote


def test_remote_passes_when_result_true() -> None:
    _verify(_remote(_ok()))


def test_remote_rejects_when_result_false() -> None:
    with pytest.raises(CaptchaInvalidError):
        _verify(_remote(_ok(result=False, code=3)))


def test_remote_rejects_expired_token() -> None:
    """data.code=2：鉴权通过但 token 已过期，仍然算失败。"""
    with pytest.raises(CaptchaInvalidError):
        _verify(_remote(_ok(result=False, code=2)))


def test_remote_code_1_rejected_by_default() -> None:
    """data.code=1（token 存在但鉴权不通过）默认拒绝：不能让 IP 绑定形同虚设。"""
    with pytest.raises(CaptchaInvalidError):
        _verify(_remote(_ok(result=False, code=1)))


def test_remote_code_1_allowed_when_lenient() -> None:
    """打开 VAPTCHA_ALLOW_CODE_1 才放行 code=1，兼容 IP 漂移的老宽带。"""

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok(result=False, code=1))

    service = VaptchaService(
        _settings(VAPTCHA_ALLOW_CODE_1=True), transport=httpx.MockTransport(handle)
    )
    _verify(service)


def test_remote_envelope_error_is_upstream_failure() -> None:
    """外层 code≠0 是我们自己的配置问题（vid/vkey 错），不是用户输错。"""
    with pytest.raises(CaptchaUpstreamError):
        _verify(_remote({"code": 1001, "msg": "invalid vid"}))


def test_remote_unreachable_fails_closed() -> None:
    with pytest.raises(CaptchaUpstreamError):
        _verify(_remote(_ok(), status=0))


def test_remote_http_500_fails_closed() -> None:
    with pytest.raises(CaptchaUpstreamError):
        _verify(_remote(_ok(), status=500))


def test_remote_sends_official_json_payload() -> None:
    """必须按 V4 契约发 JSON：vid/vkey/token/knock/dfu/ip，且 IP 取 SDK 签名值。"""
    captured: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode()
        captured["ctype"] = request.headers.get("content-type", "")
        return httpx.Response(200, json=_ok())

    service = VaptchaService(_settings(), transport=httpx.MockTransport(handle))
    _verify(service, CaptchaPayload(token="tok", knock="kno", dfu="dfu1", ip="5.6.7.8"))

    assert "application/json" in captured["ctype"]
    assert json.loads(captured["body"]) == {
        "vid": VID,
        "vkey": VKEY,
        "token": "tok",
        "knock": "kno",
        "dfu": "dfu1",
        "ip": "5.6.7.8",
    }


# ------------------------------------------------------------------ local


def test_local_accepts_correct_signature() -> None:
    # 与 _verify 默认的 client_ip 一致：token 是按那个 IP 签的
    token = _signed_token(ip="1.2.3.4")
    _verify(_local(), CaptchaPayload(token=token, knock="kno"))


def test_local_rejects_expired_token() -> None:
    token = _signed_token(age=3600)  # 超出默认 300s 窗口
    with pytest.raises(CaptchaInvalidError):
        _verify(_local(), CaptchaPayload(token=token, knock="kno"))


def test_local_rejects_tampered_signature() -> None:
    token = _signed_token()
    with pytest.raises(CaptchaInvalidError):
        _verify(_local(), CaptchaPayload(token=token[:-4] + "0000", knock="kno"))


def test_local_rejects_malformed_token() -> None:
    with pytest.raises(CaptchaInvalidError):
        _verify(_local(), CaptchaPayload(token="not-a-token", knock="kno"))


def test_local_rejects_ip_mismatch() -> None:
    """token 是拿 A 的 IP 签的，服务器拿到 B 的 IP 就该失败 —— 这正是 IP 绑定。"""
    token = _signed_token(ip="9.9.9.9")
    with pytest.raises(CaptchaInvalidError):
        _verify(_local(), CaptchaPayload(token=token, knock="kno"), ip="1.2.3.4")


def test_local_token_is_single_use() -> None:
    """同一张票只能过一次：官方文档明确提示的有效期内重放风险。"""
    token = _signed_token(ip="1.2.3.4")
    service = _local()
    captcha = CaptchaPayload(token=token, knock="kno")
    _verify(service, captcha)  # 第一次通过
    with pytest.raises(CaptchaInvalidError):
        _verify(service, captcha)  # 第二次被 Redis 标记挡住


# ------------------------------------------------------------------ 路由层


def _override_vaptcha(service: VaptchaService) -> None:
    from app.api.deps import get_vaptcha_service

    app.dependency_overrides[get_vaptcha_service] = lambda: service


def test_send_code_rejects_missing_captcha_when_enabled() -> None:
    """服务端开了人机验证，没带票据 → 400 / 11400，验证码根本不发。"""
    _override_vaptcha(_remote(_ok()))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/auth/verification-code",
            json={"email": "sr-test-vaptcha@example.com"},
        )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 11400
    # 11400 与邮箱验证码共用，但文案必须是"人机验证"，不能让用户去查邮件
    assert "人机验证" in body["message"]


def test_send_code_upstream_failure_maps_504() -> None:
    """验签上游挂了 → 504 / 21504，验证码照样不发（fail-closed）。"""
    _override_vaptcha(_remote(_ok(), status=0))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/auth/verification-code",
            json={"email": "sr-test-vaptcha@example.com", "token": "tok", "knock": "kno"},
        )
    assert resp.status_code == 504
    assert resp.json()["code"] == 21504


def test_send_code_without_captcha_when_disabled() -> None:
    """未配置 VAPTCHA：老客户端不带任何票据字段，行为不变。

    不依赖 Redis/SMTP 是否真的配置好，只断言"没有因为缺票据被 11400"。
    """
    _override_vaptcha(_remote({}, configured=False))
    with TestClient(app) as client:
        resp = client.post("/api/v1/auth/verification-code", json={"email": "not-an-email"})
    assert resp.json()["code"] != 11400
