"""pytest 公共夹具：客户端 + LLM 桩（成功/失败两种）。

桩只实现被调用到的方法（invoke / astream_events），与 LCEL 链同构，
通过 dependency_overrides 注入，生产代码零改动。
"""

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from app.core.config import get_settings
from app.main import app
from app.services.llm_service import LLMService, get_llm_service
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk


class StubChain:
    """逐字产出的桩链，事件结构与 LangChain 的 astream_events 一致。"""

    def __init__(self, answer: str = "这是桩回答") -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    def invoke(self, inputs: dict[str, Any]) -> str:
        self.calls.append(inputs)
        return self.answer

    async def astream_events(
        self, inputs: dict[str, Any], version: str = "v2"
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(inputs)
        for piece in self.answer:
            yield {
                "event": "on_chat_model_stream",
                "name": "ChatOpenAI",
                "data": {"chunk": AIMessageChunk(content=piece)},
            }
        yield {
            "event": "on_chat_model_end",
            "name": "ChatOpenAI",
            "data": {
                "output": AIMessage(
                    content=self.answer,
                    usage_metadata={
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "total_tokens": 33,
                    },
                )
            },
        }


class ExplodingChain:
    """模拟上游故障：调用即抛异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def invoke(self, inputs: dict[str, Any]) -> str:
        raise self.exc

    async def astream_events(
        self, inputs: dict[str, Any], version: str = "v2"
    ) -> AsyncIterator[dict[str, Any]]:
        # 空块会被服务层过滤，等价于"还没产出就失败"
        yield {"event": "on_chat_model_stream", "data": {"chunk": AIMessageChunk(content="")}}
        raise self.exc


def _make_service(chain: Any, configured: bool) -> LLMService:
    """用真实配置构造服务，再替换掉链 —— 提示词解析等逻辑保持真实行为。"""
    service = LLMService(get_settings())
    service._chain = chain  # noqa: SLF001
    service.configured = configured
    service.model = "stub-model"
    service.base_url = ""
    return service


@pytest.fixture()
def client() -> TestClient:
    """测试客户端：with 语句触发 lifespan，结束后清理依赖覆盖。"""
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def stub_llm() -> Callable[..., StubChain]:
    """安装成功的桩：stub_llm(answer=..., configured=...) → StubChain。"""

    def install(answer: str = "这是桩回答", configured: bool = True) -> StubChain:
        chain = StubChain(answer)
        service = _make_service(chain if configured else None, configured)
        app.dependency_overrides[get_llm_service] = lambda: service
        return chain

    return install


@pytest.fixture()
def failing_llm() -> Callable[..., None]:
    """安装失败的桩：上游异常模拟。"""

    def install(exc: Exception | None = None) -> None:
        chain = ExplodingChain(exc or RuntimeError("upstream boom"))
        app.dependency_overrides[get_llm_service] = lambda: _make_service(chain, True)

    return install
