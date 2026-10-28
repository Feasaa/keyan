"""全局异常处理：把四类异常收敛成同一个响应结构。

四类，处理方式完全不同：

    AppError                 可预期。状态码和文案由错误码决定，不记堆栈
    RequestValidationError   参数校验。422 + 逐字段错误，方便前端定位
    HTTPException            框架抛出（404 路由不存在、405 方法不对）
    Exception                兜底。500 + 堆栈进日志，**响应里绝不带原文**

最后那个兜底 handler 必须存在。没有它，未预期异常会走 Starlette 默认的
纯文本 500，既不是统一结构，还会在 DEBUG 模式下把堆栈直接返回给客户端。
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import current_trace_id
from app.core.errors import AppError, ErrorCode
from app.core.response import fail

logger = logging.getLogger(__name__)

# 框架抛的 HTTPException 只有状态码，这里补上对应的业务码
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.INVALID_PARAM,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    500: ErrorCode.INTERNAL_ERROR,
    502: ErrorCode.BAD_GATEWAY,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.LLM_TIMEOUT,
}


def _json_response(
    error_code: ErrorCode,
    message: str | None = None,
    data: Any = None,
) -> JSONResponse:
    payload = fail(error_code, message, data)
    return JSONResponse(
        status_code=error_code.http_status,
        content=payload.model_dump(),
        headers={},  # trace_id 由中间件统一写进响应头
    )


def _field_errors(exc: RequestValidationError) -> list[dict[str, str]]:
    """把 pydantic 的校验错误整理成 {field, message}。

    loc 的第一段是 body / query / path，对前端没有意义，去掉；
    剩下的是字段路径，嵌套结构用点号连接。
    """
    details: list[dict[str, str]] = []
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", ())]
        if len(loc) > 1:
            field = ".".join(loc[1:])
        else:
            field = loc[0] if loc else "request"
        details.append({"field": field, "message": error.get("msg", "")})
    return details


async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    code = exc.error_code
    # 5xx 说明是我们或依赖的问题，需要排查；4xx 是调用方用错了，warning 足够
    log = logger.error if code.http_status >= 500 else logger.warning
    log(
        "业务异常 code=%s path=%s trace_id=%s message=%s",
        code.code,
        request.url.path,
        current_trace_id(),
        exc.message,
    )
    return _json_response(code, exc.message, exc.data)


async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    code = ErrorCode.VALIDATION_FAILED
    logger.info("参数校验失败 path=%s trace_id=%s", request.url.path, current_trace_id())
    return _json_response(code, data={"errors": _field_errors(exc)})


async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_TO_CODE.get(exc.status_code)
    if code is None:
        code = ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.INVALID_PARAM
    # 不用 exc.detail：框架给的是 "Not Found" 这类英文，对用户没价值
    return _json_response(code)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """兜底：日志留全量堆栈，响应只给中性文案。

    exc_info=exc 而不是 logger.exception：
    这个 handler 不在 except 块里被调用，sys.exc_info() 是空的，
    用 logger.exception 会打出 "NoneType: None"，等于没留堆栈。
    """
    logger.error(
        "未处理异常 path=%s trace_id=%s",
        request.url.path,
        current_trace_id(),
        exc_info=exc,
    )
    return _json_response(ErrorCode.INTERNAL_ERROR)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, handle_validation_error)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, handle_http_exception)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, handle_unexpected_error)
