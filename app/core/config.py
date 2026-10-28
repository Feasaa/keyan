"""全局配置：环境相关的值集中在此。

优先级：真实环境变量 > .env 文件 > 本文件默认值。
"""

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.prompts import PromptKey


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
    # 版本只体现在 URL 契约里，目录结构不再嵌套 v1
    API_PREFIX: str = "/api/v1"
    DOCS_URL: str = "/docs"
    OPENAPI_URL: str = "/api/v1/openapi.json"

    # 跨域：环境变量里用 JSON 数组写法，如 CORS_ORIGINS=["http://localhost:5173"]
    CORS_ORIGINS: list[str] = ["*"]

    # 服务监听
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # SSE：ping 注释的间隔（秒）。要小于网关/负载均衡的空闲超时（常见 60s）
    SSE_PING_SECONDS: float = 15.0

    # LLM（LangChain，OpenAI 兼容网关）
    # 密钥为空 = 功能关闭，服务照常启动，/api/v1/chat/* 返回 503
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = "deepseek-flash"
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = 2
    LLM_TEMPERATURE: float = 0.7

    # 提示词：配置里只存"用哪个 id"，正文由 app/prompts/files/*.md 提供。
    # 这样切提示词变体（灰度/租户）只改环境变量，不必动代码。
    PROMPT_CHAT_SYSTEM_ID: str = PromptKey.CHAT_SYSTEM.value

    # 雪花 ID 机器号（0-1023）。单实例填 0 即可；
    # 多实例部署时每台必须不同，否则会生成完全相同的 ID。
    # 纪元写死在 app/core/snowflake.py，不开放配置 —— 改纪元会让新旧 ID 撞车。
    SNOWFLAKE_WORKER_ID: int = 0

    # ---- 数据库（MySQL 8.4，见 deploy/）----
    DB_HOST: str = "127.0.0.1"
    DB_PORT: int = 3306
    DB_USER: str = "app"
    DB_PASSWORD: str = ""
    DB_NAME: str = "agent_chat"
    DB_ECHO: bool = False

    # 连接池：单实例并发上限 ≈ POOL_SIZE + MAX_OVERFLOW
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # 小于 MySQL 的 wait_timeout（默认 8 小时），主动换掉可能被服务端关闭的连接
    DB_POOL_RECYCLE_SECONDS: int = 3600

    @property
    def db_configured(self) -> bool:
        """没配密码视为"数据库功能关闭"，应用照常启动。"""
        return bool(self.DB_PASSWORD)

    @property
    def database_url(self) -> str:
        """异步 DSN。

        密码必须 quote_plus：密码里出现 @ / : / # 会把 DSN 拆坏，
        报出来的错还跟密码无关（通常是一句看不懂的 "invalid port"）。
        """
        return (
            f"mysql+aiomysql://{quote_plus(self.DB_USER)}:{quote_plus(self.DB_PASSWORD)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """配置工厂：全局单例，只解析一次。"""
    return Settings()


settings = get_settings()
