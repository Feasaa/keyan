"""对话接口的数据契约：一次性响应与 SSE 流式帧。"""

from pydantic import BaseModel, Field, field_validator

# 历史消息只允许 user/assistant：system 提示词只能由服务端注入，防提示注入
ROLE_WHITELIST = {"user", "assistant"}


class ChatMessage(BaseModel):
    role: str = Field(..., description="角色：user 或 assistant", examples=["user"])
    content: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="消息内容，非空且限长",
        examples=["你好"],
    )

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        normalized = v.strip().lower()
        if normalized not in ROLE_WHITELIST:
            raise ValueError(f"role 只允许 {sorted(ROLE_WHITELIST)}，收到：{v!r}")
        return normalized


class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="本轮用户提问",
        examples=["用一句话介绍 LangChain"],
    )
    conversation_id: str | None = Field(
        None,
        description=(
            "接着哪个会话聊（雪花 ID 字符串）。"
            "不传 = 新建会话；传了 = 由服务端加载该会话历史后继续。"
        ),
        examples=["373814368841760768"],
    )
    # 刻意**没有** history 字段。
    #
    # 上下文只从数据库按 conversation_id 加载。允许客户端传历史，就等于
    # 让浏览器决定喂给模型什么 —— 伪造几条 assistant 消息就能把模型带偏，
    # 而请求参数看起来完全合法，服务端日志里查不出异常。
    # 直接不定义这个字段：客户端多传会被丢弃（Pydantic 默认忽略额外字段），
    # 既不会 422 打断老前端，也不可能生效。

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        if not value:
            return None
        # 只做形状校验：19 位雪花 ID 是纯数字。真正的归属校验在服务层，
        # 那里查不到就是 404，不会因为这里放过一个不存在的 ID 而出问题。
        if not value.isdigit():
            raise ValueError(f"conversation_id 必须是雪花 ID 字符串，收到：{v!r}")
        return value


class ChatResponse(BaseModel):
    answer: str = Field(..., description="模型回答")
    model: str = Field(..., description="实际使用的模型名", examples=["deepseek-chat"])
    usage: dict[str, int] | None = Field(
        None,
        description="令牌用量（网关支持时返回）",
        examples=[{"prompt_tokens": 120, "completion_tokens": 80}],
    )
    conversation_id: str = Field(..., description="会话 ID（雪花字符串）")
    message_id: str = Field(..., description="本轮助手消息 ID，评价接口要用它")


class ChatStatusResponse(BaseModel):
    configured: bool = Field(..., description="是否已配置 API 密钥")
    model: str = Field(..., description="配置的模型名", examples=["deepseek-chat"])
    base_url: str = Field(..., description="网关地址（未配置时为空串）")
    prompt_id: str = Field(..., description="当前使用的提示词 id", examples=["chat_system"])
    prompt_version: int = Field(..., description="提示词版本，排障时用于回溯", examples=[1])


class StreamChunk(BaseModel):
    """SSE 增量帧：event=delta。"""

    delta: str = Field(..., description="本次增量文本")


class StreamMeta(BaseModel):
    """SSE 元信息帧：event=meta。结束前一次性给出，供客户端统计用量。

    会话 ID 也在这里再给一次：`start` 帧已经给过，但断线续传的客户端
    可能是从中途接上的（没看到 start），多给一份不占什么，少给就得多一次请求。

    为什么不用 `event=done` 带这些字段：`done` 是纯文本终止符（SSE 的收尾约定），
    往里塞 JSON 会让"看到 done 就断流"的客户端解析出错。
    """

    model: str = Field(..., description="实际使用的模型名", examples=["deepseek-chat"])
    prompt_tokens: int | None = Field(None, description="输入令牌数（网关不返回时为空）")
    completion_tokens: int | None = Field(None, description="输出令牌数（网关不返回时为空）")
    conversation_id: str = Field(..., description="会话 ID（雪花字符串）")
    message_id: str = Field(..., description="本轮助手消息 ID，评价接口要用它")


class StreamStart(BaseModel):
    """SSE 首帧：event=start，**没有 id**。

    为什么首帧要单独发：断线续传要知道"续哪一条"，而这个 ID 原本只在
    `meta` 帧（流结束时）才给。断线时客户端还没拿到 meta，就无从续起。
    所以开工前先把身份给出去，之后的 delta 帧才带 `id:`。

    不带 `id:` 是有意的：客户端在收到第一个 delta 之前断开时，
    `Last-Event-ID` 是空的，续传就从头的 `0-0` 开始 —— 正好是想要的语义。
    """

    conversation_id: str = Field(..., description="会话 ID（雪花字符串）")
    message_id: str = Field(..., description="本轮助手消息 ID；续传与评价都用它")


class StreamError(BaseModel):
    """SSE 错误帧：event=error。流已开始后无法再改 HTTP 状态码，只能靠事件传递。"""

    code: int = Field(..., description="业务错误码，与 JSON 接口同一套", examples=[22502])
    message: str = Field(..., description="中性错误摘要")
