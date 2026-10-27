"""全局配置：环境相关的值集中在此。

优先级：真实环境变量 > .env 文件 > 本文件默认值。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.prompts import ASSISTANT_PROMPT, SYSTEM_PROMPT


class Settings(BaseSettings):
    """项目配置项，字段名即环境变量名（区分大小写）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # 应用元信息
    PROJECT_NAME: str = "learn-fastapi"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

    # 接口与文档
    API_V1_PREFIX: str = "/api/v1"
    DOCS_URL: str = "/docs"
    OPENAPI_URL: str = "/api/v1/openapi.json"

    # 跨域：环境变量里用 JSON 数组写法，如 CORS_ORIGINS=["http://localhost:5173"]
    CORS_ORIGINS: list[str] = ["*"]

    # 服务监听
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # LLM（LangChain，OpenAI 兼容网关）
    # 密钥为空 = 功能关闭，服务照常启动，/api/v1/llm/* 返回 503
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "deepseek-flash"
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = 2
    LLM_TEMPERATURE: float = 0.7

    # 提示词：默认取 app/prompts/ 下的定义，可用环境变量覆盖
    LLM_SYSTEM_PROMPT: str = SYSTEM_PROMPT
    LLM_ASSISTANT_PROMPT: str = ASSISTANT_PROMPT


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """配置工厂：全局单例，只解析一次。"""
    return Settings()


settings = get_settings()
