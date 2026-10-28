"""CORS 配置测试。

锁住的不变量只有一条：**凭证与通配符不能同时放开**。

容易搞错的地方是"放开之后会怎样"。Starlette 在 credentials=True 时会把请求的
Origin 原样回显（`allow_explicit_origin`），响应头语法合法、浏览器照收 ——
所以问题不是"被浏览器拒绝"，而是"任意站点都被放行"。
也就是说这类配置错误**不会报错**，只会安静地多出一个越权读取面，
因此必须由测试来守。
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from app.core.config import get_settings
from app.main import create_app
from fastapi.testclient import TestClient

LIVE = "/api/v1/health"
ORIGIN = "https://app.example.com"
EVIL = "https://evil.example"


@contextmanager
def _app_with(monkeypatch: pytest.MonkeyPatch, origins: list[str]) -> Iterator[TestClient]:
    """按给定 CORS_ORIGINS 重建一个应用。

    `create_app()` 在调用时读 `settings`，所以替换模块级对象即可 ——
    不用改环境变量、不用重载模块，也就不会污染其他用例。
    """
    patched = get_settings().model_copy(update={"CORS_ORIGINS": origins})
    monkeypatch.setattr("app.main.settings", patched)
    # 不进 lifespan：只验中间件产出的响应头，不需要提示词/连接池
    yield TestClient(create_app())


def test_wildcard_drops_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """通配符场景：允许跨域，但不允许带凭证。"""
    with _app_with(monkeypatch, ["*"]) as client:
        response = client.get(LIVE, headers={"Origin": EVIL})

    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_explicit_origin_allows_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """收敛到具体域名后才放开凭证 —— 这才是生产的正确形态。"""
    with _app_with(monkeypatch, [ORIGIN]) as client:
        response = client.get(LIVE, headers={"Origin": ORIGIN})

    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unknown_origin_gets_no_allow_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """配了具体域名后，陌生站点连"允许跨域"这个头都不该拿到。

    少了这条断言，"allow_credentials 关了但 allow_origins 还是通配符"
    这种改一半的状态就会被漏过去。
    """
    with _app_with(monkeypatch, [ORIGIN]) as client:
        response = client.get(LIVE, headers={"Origin": EVIL})

    assert "access-control-allow-origin" not in response.headers


def test_wildcard_never_grants_credentials_to_any_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """核心不变量：通配符下，任何 Origin 都不能拿到凭证授权。

    这正是回归点。旧配置（`["*"]` + `allow_credentials=True`）会回显 Origin
    并附上 `Allow-Credentials: true`，浏览器不会报错，
    等于任何站点都能读到登录用户的接口响应。

    也覆盖 `["*", ORIGIN]` —— 手写配置时很容易顺手把通配符和真实域名一起列上，
    结果仍然走通配符分支，表现为"我明明写了域名，为什么还是不生效"。
    """
    for origins in (["*"], ["*", ORIGIN]):
        with _app_with(monkeypatch, origins) as client:
            response = client.get(LIVE, headers={"Origin": EVIL})

        assert response.headers.get("access-control-allow-credentials") != "true", (
            f"通配符下不得开放凭证，origins={origins}"
        )


def test_trace_header_is_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Request-ID 要暴露给 JS。

    CORS 默认只放行少数几个安全响应头，不显式 expose 的话
    前端 `response.headers.get("X-Request-ID")` 恒为 null ——
    用户报障时提供不出 trace_id，服务端日志就只能靠时间戳去猜。
    """
    with _app_with(monkeypatch, [ORIGIN]) as client:
        response = client.get(LIVE, headers={"Origin": ORIGIN})

    exposed = response.headers["access-control-expose-headers"]
    assert "X-Request-ID" in exposed
