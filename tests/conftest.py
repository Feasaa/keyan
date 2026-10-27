"""pytest 公共夹具。"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client() -> TestClient:
    """测试客户端：with 语句触发 lifespan，结束后清理依赖覆盖。"""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
