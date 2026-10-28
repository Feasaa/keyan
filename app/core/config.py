"""全局配置：环境相关的值集中在此。

优先级：真实环境变量 > .env 文件 > 本文件默认值。
"""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.paths import resolve_path
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

    # ---- 提示词 ----
    # 正文是内容、不是代码，所以放在仓库根的 prompts/ 而不是 app/ 包里：
    # 改一句话不该触发重新打包，运维/产品也不该进源码目录才能改。
    # 相对路径按仓库根解析；容器里挂载到别处时填绝对路径（如 /app/prompts）。
    PROMPTS_DIR: str = "prompts"
    # 配置里只存"用哪个 id"，这样切提示词变体（灰度/租户）只改环境变量。
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

    # 迁移专用账号（DDL 权限）。运行时账号 DB_USER 只给 DML，
    # 这样即使某天出现注入、拿到了 SQL 执行权，也删不掉表、改不了结构。
    # 本地开发可以不配，不配时迁移沿用 DB_USER。
    DB_MIGRATION_USER: str = "migrator"
    DB_MIGRATION_PASSWORD: str = ""

    # 连接池：单实例并发上限 ≈ POOL_SIZE + MAX_OVERFLOW
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # 小于 MySQL 的 wait_timeout（默认 8 小时），主动换掉可能被服务端关闭的连接
    DB_POOL_RECYCLE_SECONDS: int = 3600

    # ---- Redis（验证码 / 限流；见 deploy/）----
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 3.0

    # ---- 鉴权 ----
    # 密钥为空 = 鉴权功能关闭：/api/v1/auth/* 返回 503，其余接口不受影响。
    # 生产必须显式配置，绝不能给默认值 —— 默认密钥等于没有密钥。
    # 生成方式：python -c "import secrets; print(secrets.token_urlsafe(48))"
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    # access 短、refresh 长。access 不落库，泄露后最多被用 15 分钟；
    # 想立刻踢人只能靠 refresh 撤销，所以 access 不能设成几天。
    ACCESS_TOKEN_TTL_SECONDS: int = 15 * 60
    REFRESH_TOKEN_TTL_SECONDS: int = 30 * 24 * 3600

    # ---- 邮件（QQ SMTP）----
    # 465 走隐式 SSL，587 走 STARTTLS，由端口自动判定，不再单独开关。
    SMTP_HOST: str = "smtp.qq.com"
    SMTP_PORT: int = 465
    SMTP_USER: str = ""  # 完整发件地址，如 123456@qq.com
    SMTP_PASSWORD: str = ""  # QQ 邮箱的"授权码"，不是登录密码
    SMTP_FROM_NAME: str = "AI 助手"
    SMTP_TIMEOUT_SECONDS: float = 10.0

    # ---- 邮箱验证码 ----
    # TTL 与重发冷却分开：TTL 是"这个码多久失效"，冷却是"多久能再要一个"。
    VERIFICATION_CODE_TTL_SECONDS: int = 5 * 60
    VERIFICATION_CODE_RESEND_SECONDS: int = 60
    # 同一验证码允许试错次数，超了直接作废 —— 否则 6 位数字可以在有效期内穷举
    VERIFICATION_CODE_MAX_ATTEMPTS: int = 5
    # 两层配额：按邮箱防单点轰炸，按 IP 防换邮箱刷
    VERIFICATION_DAILY_LIMIT_PER_EMAIL: int = 10
    VERIFICATION_HOURLY_LIMIT_PER_IP: int = 20

    @property
    def prompts_dir(self) -> Path:
        """提示词目录的绝对路径（相对 PROMPTS_DIR 按仓库根解析）。"""
        return resolve_path(self.PROMPTS_DIR)

    @property
    def db_configured(self) -> bool:
        """没配密码视为"数据库功能关闭"，应用照常启动。"""
        return bool(self.DB_PASSWORD)

    def _dsn(self, user: str, password: str) -> str:
        """拼异步 DSN。

        账号密码必须 quote_plus：密码里出现 @ / : / # 会把 DSN 拆坏，
        报出来的错还跟密码无关（通常是一句看不懂的 "invalid port"）。
        """
        return (
            f"mysql+aiomysql://{quote_plus(user)}:{quote_plus(password)}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset=utf8mb4"
        )

    @property
    def database_url(self) -> str:
        """运行时 DSN：只有 DML 权限。"""
        return self._dsn(self.DB_USER, self.DB_PASSWORD)

    @property
    def migration_url(self) -> str:
        """迁移 DSN：需要 DDL 权限。

        没配迁移账号时退回运行时账号 —— 本地开发图方便，
        但那种情况下 app 账号必然带着 DDL 权限，生产不要这么干。
        """
        if not self.DB_MIGRATION_PASSWORD:
            return self.database_url
        return self._dsn(self.DB_MIGRATION_USER, self.DB_MIGRATION_PASSWORD)

    @property
    def redis_configured(self) -> bool:
        return bool(self.REDIS_PASSWORD)

    @property
    def redis_url(self) -> str:
        """redis:// 形式。密码同样要 quote_plus，理由同 database_url。"""
        auth = f":{quote_plus(self.REDIS_PASSWORD)}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def jwt_configured(self) -> bool:
        return bool(self.JWT_SECRET)

    @property
    def mail_configured(self) -> bool:
        """发件账号和授权码都齐了才算可用。"""
        return bool(self.SMTP_USER and self.SMTP_PASSWORD)

    @property
    def smtp_use_ssl(self) -> bool:
        """465 是隐式 SSL（连上就加密），587 是 STARTTLS（先明文再升级）。"""
        return self.SMTP_PORT == 465


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """配置工厂：全局单例，只解析一次。"""
    return Settings()


settings = get_settings()
