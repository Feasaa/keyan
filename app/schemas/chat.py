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
    # 上限 20 条：成本与上下文窗口的第一道闸
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=20,
        description="可选的多轮历史，最多 20 条",
    )


class ChatResponse(BaseModel):
    answer: str = Field(..., description="模型回答")
    model: str = Field(..., description="实际使用的模型名", examples=["deepseek-chat"])
    usage: dict[str, int] | None = Field(
        None,
        description="令牌用量（网关支持时返回）",
        examples=[{"prompt_tokens": 120, "completion_tokens": 80}],
    )


class ChatStatusResponse(BaseModel):
    configured: bool = Field(..., description="是否已配置 API 密钥")
    model: str = Field(..., description="配置的模型名", examples=["deepseek-chat"])
    base_url: str = Field(..., description="网关地址（未配置时为空串）")


class StreamChunk(BaseModel):
    """SSE 增量帧：event=delta。"""

    delta: str = Field(..., description="本次增量文本")


class StreamError(BaseModel):
    """SSE 错误帧：event=error。流已开始后无法再改 HTTP 状态码，只能靠事件传递。"""

    code: int = Field(..., description="等价 HTTP 状态码", examples=[502])
    message: str = Field(..., description="中性错误摘要")
