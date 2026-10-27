"""业务异常：service 层不碰 HTTP，错误语义由接口层翻译成状态码。"""


class ServiceError(Exception):
    """业务异常基类。"""


class LLMNotConfiguredError(ServiceError):
    """LLM 未配置（密钥为空）：功能整体关闭，接口层映射 503。"""

    def __init__(self) -> None:
        super().__init__("LLM 未配置：请设置 LLM_API_KEY 后重启服务")


class LLMUpstreamError(ServiceError):
    """上游网关故障（密钥无效 / 限流 / 超时 / 网络不可达）。

    只暴露中性摘要，原始异常挂 __cause__ 供服务端日志溯源。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"LLM 上游服务异常：{reason}")
