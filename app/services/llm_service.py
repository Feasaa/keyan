"""LLM 服务：LangChain LCEL 链（prompt | model | parser）的封装。

客户端是进程级单例：内部持有 httpx 连接池，每请求新建会导致连接风暴。
未配置密钥时不构建链，chat() 抛 LLMNotConfiguredError，由接口层映射 503。
"""

from functools import lru_cache

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableSerializable
from langchain_openai import ChatOpenAI

from app.core.config import Settings, get_settings
from app.prompts import compose_system_prompt
from app.schemas.llm import ChatRequest
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

    def chat(self, payload: ChatRequest) -> tuple[str, str]:
        """执行一次对话，返回 (answer, model_name)。

        上游异常统一包成 LLMUpstreamError（原异常挂 __cause__ 供日志溯源），
        避免密钥、网关地址、堆栈透传给客户端。
        """
        if self._chain is None:
            raise LLMNotConfiguredError()

        try:
            answer: str = self._chain.invoke(
                {
                    "history": [(msg.role, msg.content) for msg in payload.history],
                    "message": payload.message,
                }
            )
        except Exception as exc:
            raise LLMUpstreamError(type(exc).__name__) from exc
        return answer, self.model


@lru_cache(maxsize=1)
def get_llm_service() -> LLMService:
    """LLM 服务工厂：带缓存的全局单例。"""
    return LLMService(get_settings())
