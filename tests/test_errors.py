"""统一返回、错误码、全局异常处理的契约测试。

这几条是"项目骨架"级别的约定，一旦被改坏，
所有接口的响应结构都会跟着变，前端要全量改 —— 所以必须锁死。
"""

import pytest
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.response import ApiResponse, ok
from app.main import create_app
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

CHAT = "/api/v1/chat"


@pytest.fixture
def probe_client() -> TestClient:
    """带探针路由的独立应用：专门用来触发各类异常。"""
    app = create_app()

    @app.get("/probe/boom")
    async def boom() -> None:
        raise RuntimeError("内部细节：密码 sk-secret-leak / 表 users 不存在")

    @app.get("/probe/not-found")
    async def not_found() -> None:
        raise NotFoundError("会话不存在或已删除")

    @app.get("/probe/typed")
    async def typed(q: int = Query(..., description="必须是整数")) -> int:
        return q

    # raise_server_exceptions=False：让 500 走真实响应，而不是把异常抛给测试
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------- 响应结构


def test_success_envelope(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()

    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["data"]["status"] == "ok"


def test_envelope_helper_matches_model() -> None:
    payload = ok({"a": 1})

    assert isinstance(payload, ApiResponse)
    assert payload.code == ErrorCode.OK.code


# ---------------------------------------------------------------- trace_id


def test_trace_id_generated_and_returned(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    trace_id = response.headers["X-Request-ID"]
    assert trace_id
    assert response.json()["trace_id"] == trace_id


def test_trace_id_echoes_client_value(client: TestClient) -> None:
    """客户端带了就沿用，便于前端/网关串起同一条链路。"""
    response = client.get("/api/v1/health", headers={"X-Request-ID": "trace-abc-123"})

    assert response.headers["X-Request-ID"] == "trace-abc-123"
    assert response.json()["trace_id"] == "trace-abc-123"


def test_trace_id_differs_per_request(client: TestClient) -> None:
    first = client.get("/api/v1/health").json()["trace_id"]
    second = client.get("/api/v1/health").json()["trace_id"]

    assert first != second


# ---------------------------------------------------------------- 错误路径


def test_unknown_route_uses_envelope(probe_client: TestClient) -> None:
    response = probe_client.get("/api/v1/nonexistent")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 10404
    assert body["data"] is None
    assert body["trace_id"]


def test_wrong_method_uses_envelope(probe_client: TestClient) -> None:
    """GET 一个只接受 POST 的路径：405，不是 404，也不是 500。"""
    response = probe_client.get(CHAT)

    assert response.status_code == 405
    assert response.json()["code"] == 10405


def test_validation_error_lists_fields(probe_client: TestClient) -> None:
    response = probe_client.get("/probe/typed", params={"q": "abc"})

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 10422
    errors = body["data"]["errors"]
    assert errors[0]["field"] == "q"
    assert errors[0]["message"]


def test_missing_required_field(probe_client: TestClient) -> None:
    response = probe_client.get("/probe/typed")

    assert response.status_code == 422
    assert response.json()["data"]["errors"][0]["field"] == "q"


def test_app_error_keeps_custom_message(probe_client: TestClient) -> None:
    response = probe_client.get("/probe/not-found")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == 10404
    assert body["message"] == "会话不存在或已删除"


def test_unexpected_error_hides_internals(probe_client: TestClient) -> None:
    """兜底 500：日志里有堆栈，响应里只有中性文案。"""
    response = probe_client.get("/probe/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == 10500
    assert body["message"] == "服务内部异常"
    # 绝不能把异常原文、密钥、表名带出去
    assert "sk-secret-leak" not in response.text
    assert "RuntimeError" not in response.text
    assert "users" not in response.text


# ---------------------------------------------------------------- 错误码本身


@pytest.mark.parametrize("code", [c for c in ErrorCode if c is not ErrorCode.OK])
def test_code_encoding_rule(code: ErrorCode) -> None:
    """编码规则 X M SSS：5 位，后三位就是 HTTP 状态码。

    这条规则一旦破坏，"看码就知道大概什么问题"的能力就没了。
    """
    text = str(code.code)
    assert len(text) == 5, f"{code.name} 不是 5 位：{text}"
    assert text[0] in ("1", "2"), f"{code.name} 首位应为 1（调用方）或 2（服务端）"
    assert int(text[-3:]) == code.http_status, f"{code.name} 后三位与 http_status 不一致"


def test_error_code_unique() -> None:
    codes = [c.code for c in ErrorCode]
    assert len(codes) == len(set(codes)), "错误码有重复"


def test_app_error_str_includes_code() -> None:
    exc = AppError(ErrorCode.CONFLICT, "已存在同名会话")

    assert str(exc) == "[10409] 已存在同名会话"
    assert exc.error_code.http_status == 409


def test_app_error_defaults_to_code_message() -> None:
    exc = NotFoundError()

    assert exc.message == ErrorCode.NOT_FOUND.message


def test_api_response_openapi_schema(client: TestClient) -> None:
    """文档里也要能看出统一结构，否则前端联调时靠猜。"""
    schema = client.get("/api/v1/openapi.json").json()
    chat_schema = schema["paths"]["/api/v1/chat"]["post"]["responses"]["200"]["content"]

    assert "application/json" in chat_schema
    ref = chat_schema["application/json"]["schema"]["$ref"]
    assert ref.endswith("ApiResponse_ChatResponse_")


def test_fastapi_app_factory_returns_app() -> None:
    assert isinstance(create_app(), FastAPI)
