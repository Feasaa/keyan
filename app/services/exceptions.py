"""业务异常：service 层只表达"发生了什么"，不关心 HTTP。

所有异常都继承 AppError，由全局处理器翻译成统一响应 ——
service 里看不到一个 HTTPException，换框架也不用重写业务。
"""

from app.core.errors import AppError, ErrorCode


class ServiceError(AppError):
    """业务异常基类。

    子类必须用 default_code 声明自己的错误码；
    没声明就落到 500，等于告诉调用方"这是 bug"，属于有意为之的默认值。
    """

    default_code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        data: object | None = None,
    ) -> None:
        super().__init__(code or self.default_code, message, data=data)


class LLMNotConfiguredError(ServiceError):
    """LLM 未配置（密钥为空）：功能整体关闭，映射 503。"""

    default_code = ErrorCode.LLM_NOT_CONFIGURED

    def __init__(self) -> None:
        super().__init__("LLM 未配置：请设置 LLM_API_KEY 后重启服务")


class LLMUpstreamError(ServiceError):
    """上游网关故障（密钥无效 / 限流 / 超时 / 网络不可达）。

    只暴露中性摘要（这里给的是异常**类名**，不是异常内容），
    原始异常挂在 __cause__ 上供日志溯源 ——
    上游报错原文常带密钥前缀、内部地址，绝不能进响应体。
    """

    default_code = ErrorCode.LLM_UPSTREAM_ERROR

    def __init__(self, reason: str) -> None:
        super().__init__(f"LLM 上游服务异常：{reason}")
