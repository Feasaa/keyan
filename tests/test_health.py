"""健康检查测试：存活探针与就绪探针的语义差异。"""

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

LIVE = "/api/v1/health"
READY = "/api/v1/health/ready"


def test_liveness_ok(client: TestClient) -> None:
    response = client.get(LIVE)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert "version" in body["data"]


def test_readiness_ready(client: TestClient, stub_llm: Callable[..., Any]) -> None:
    stub_llm()

    response = client.get(READY)

    assert response.status_code == 200
    assert response.json()["data"] == {"status": "ready", "checks": {"llm": "configured"}}


def test_readiness_degraded_when_llm_missing(
    client: TestClient, stub_llm: Callable[..., Any]
) -> None:
    """未配置密钥：标记 degraded 但仍返回 200，不触发重启。"""
    stub_llm(configured=False)

    response = client.get(READY)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "status": "degraded",
        "checks": {"llm": "not_configured"},
    }
