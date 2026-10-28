"""统一响应结构。

成功和失败用同一个外层结构，前端只写一次解包逻辑：

    {"code": 0, "message": "ok", "data": {...}, "trace_id": "..."}
    {"code": 22502, "message": "LLM 上游服务异常", "data": null, "trace_id": "..."}

一个刻意的选择：**HTTP 状态码仍然是真实的**（502 就是 502），
不是"一律 200、错误码放 body"。
原因有三：
    1. 网关、监控、告警、日志采集都按 HTTP 状态码统计错误率，
       全 200 会让所有现成的可观测性工具失效；
    2. 浏览器、CDN、重试中间件都依赖状态码语义；
    3. SSE 走不了 envelope（它是事件流），两套语义并存只会更乱。
所以是"状态码表达协议层结果，code 表达业务原因"，两者并存。
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.context import current_trace_id
from app.core.errors import ErrorCode

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = Field(0, description="业务错误码，0 表示成功", examples=[0])
    message: str = Field("ok", description="给人看的提示，可直接展示", examples=["ok"])
    data: T | None = Field(None, description="业务数据；失败时为 null")
    trace_id: str = Field("", description="请求追踪 ID，报障时请提供")


def ok(data: T | None = None, message: str = "ok") -> ApiResponse[T]:
    return ApiResponse[T](
        code=ErrorCode.OK.code,
        message=message,
        data=data,
        trace_id=current_trace_id(),
    )


def fail(
    error_code: ErrorCode,
    message: str | None = None,
    data: object | None = None,
) -> ApiResponse[object]:
    """构造失败响应。

    只在无法抛异常的地方用（比如 SSE 流内 —— 那时响应头已发出，
    只能往流里写一帧）。正常的失败路径请抛 AppError，让全局处理器统一处理。
    """
    return ApiResponse[object](
        code=error_code.code,
        message=message or error_code.message,
        data=data,
        trace_id=current_trace_id(),
    )
