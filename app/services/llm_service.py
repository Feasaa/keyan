"""LLM 服务：LangChain LCEL 链（prompt | model | parser）的封装。

客户端是进程级单例：内部持有 httpx 连接池，每请求新建会导致连接风暴。
未配置密钥时不构建链，调用方拿到 LLMNotConfiguredError，由路由层映射 503。
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableSerializable
from langchain_openai import ChatOpenAI

from app.core.config import Settings, get_settings
from app.prompts import compose_system_prompt
from app.schemas.chat import ChatRequest
from app.services.exceptions import LLMNotConfiguredError, LLMUpstreamError


class LLMService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._chain: RunnableSerializable | None = None
        if settings.LLM_API_KEY:
            self._chain = self._build_chain(settings)
        self.configured = bool(settings.LLM_API_KEY)
        self.model = settings.LLM_MODEL
        self.base_url = settings.LLM_BASE_URL

    @staticmethod
    def _build_chain(settings: Settings) -> RunnableSerializable:
        prompt = ChatPromptTemplate.from_messages(
            [
                # 系统提示词与助理提示词只在服务端注入，客户端无法覆盖
                (
                    "system",
                    compose_system_prompt(
                        settings.LLM_SYSTEM_PROMPT,
                        settings.LLM_ASSISTANT_PROMPT,
                    ),
                ),
                ("placeholder", "{history}"),
                ("human", "{message}"),
            ]
        )
        model = ChatOpenAI(
            api_key=settings.LLM_API_KEY,
            model=settings.LLM_MODEL,
            temperature=settings.LLM_TEMPERATURE,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=settings.LLM_MAX_RETRIES,
            base_url=settings.LLM_BASE_URL or None,
        )
        parser = StrOutputParser()
        return prompt | model | parser

    # ------------------------------------------------------------------ 校验

    def ensure_configured(self) -> None:
        """流式响应开始后无法再改状态码，所以配置检查必须前置。"""
        if self._chain is None:
            raise LLMNotConfiguredError()

    def _chain_inputs(self, payload: ChatRequest) -> dict:
        return {
            "history": [(msg.role, msg.content) for msg in payload.history],
            "message": payload.message,
        }

    # ------------------------------------------------------------------ 调用

    def chat(self, payload: ChatRequest) -> tuple[str, str]:
        """一次性返回 (answer, model_name)。"""
        self.ensure_configured()
        chain = self._chain
        if chain is None:  # 仅为类型收窄，逻辑上不会走到
            raise LLMNotConfiguredError()

        try:
            answer: str = chain.invoke(self._chain_inputs(payload))
        except Exception as exc:
            raise LLMUpstreamError(type(exc).__name__) from exc
        return answer, self.model

    async def astream_chat(self, payload: ChatRequest) -> AsyncIterator[str]:
        """流式产出增量文本（LCEL 的 astream 逐块返回）。

        上游异常在流中途才可能出现，此时响应头已下发，只能由路由层转成 error 事件。
        """
        self.ensure_configured()
        chain = self._chain
        if chain is None:  # 仅为类型收窄
            raise LLMNotConfiguredError()

        try:
            async for chunk in chain.astream(self._chain_inputs(payload)):
                if chunk:
                    yield chunk
        except Exception as exc:
            raise LLMUpstreamError(type(exc).__name__) from exc


@lru_cache(maxsize=1)
def get_llm_service() -> LLMService:
    """LLM 服务工厂：带缓存的全局单例。"""
    return LLMService(get_settings())
