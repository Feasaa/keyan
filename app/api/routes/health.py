"""健康检查端点：存活探针（liveness）与就绪探针（readiness）。

两个探针分开，是因为它们的失败语义不同：
    liveness  失败 → 编排系统重启容器
    readiness 失败 → 仅从负载均衡摘除，不重启
合并成一个接口会导致"依赖抖动就重启实例"，把小故障放大成雪崩。

响应也套统一结构。探针只看 HTTP 状态码，不看 body，
所以包一层不影响任何编排系统；换来的是全站只有一个响应格式。
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.core.config import Settings, get_settings, settings
from app.core.errors import ErrorCode
from app.core.response import ApiResponse, fail, ok
from app.db import get_engine, get_redis
from app.schemas.health import HealthResponse, ReadinessResponse
from app.services.llm_service import LLMService, get_llm_service

logger = logging.getLogger(__name__)

router = APIRouter()

# 检查结果取值。用固定字符串而不是 bool：
# "没配" 和 "配了但连不上" 必须能分开 —— 前者是功能未启用（不该摘流量），
# 后者是故障（必须摘流量）。混成一个 False 就没法区分了。
OK = "ok"
NOT_CONFIGURED = "not_configured"
ERROR = "error"


async def _probe_db(config: Settings) -> str:
    """真跑一条 `SELECT 1`。

    为什么不复用连接池的 `pool_pre_ping`：那只在**取连接时**顺带探活，
    失败表现为业务请求报错，探针这边什么都看不到。
    就绪探针要回答的是"现在能不能连上"，必须自己发一条语句。

    用 `SELECT 1` 而不是查业务表：前者不碰任何表，
    不会被行锁、表维护、权限变更影响，测的就是"连接 + 认证"这一层。
    """
    if not config.db_configured:
        return NOT_CONFIGURED
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return OK


async def _probe_redis(config: Settings) -> str:
    """`PING` 一次。

    Redis 是验证码和登录限流的唯一存储，它挂了等于注册/登录不可用，
    所以和数据库同级看待，而不是"缓存挂了凑合能用"。
    """
    if not config.redis_configured:
        return NOT_CONFIGURED
    await get_redis().ping()
    return OK


async def _probe_llm(service: LLMService) -> str:
    """LLM 只看配置，不发真实请求。

    真调一次上游要几百毫秒到几秒，还产生费用；而"密钥配了但上游挂了"
    这件事对话接口自己会返回 503，不需要靠探针提前摘流量 ——
    摘了等于让所有实例一起下线，把小故障放大成全站不可用。
    """
    return OK if service.configured else NOT_CONFIGURED


async def _probe(
    name: str,
    make_coro: Callable[[], Awaitable[str]],
    *,
    timeout: float,
) -> str:
    """跑单个探针，把异常与超时收敛成 `error`。

    必须自己吞异常：任一依赖拒绝连接时，探针本身不能 500 ——
    500 和 503 在编排系统眼里都是"不健康"，但 500 会丢掉 checks 明细，
    排障时就只剩"探针挂了"这一句。

    超时是必须的：MySQL 在连接池耗尽或半开连接时，`connect()` 会一直挂着。
    探针被它拖住就等于没有探针，最后是编排系统的探针超时把所有实例一起摘掉。
    """
    try:
        return await asyncio.wait_for(make_coro(), timeout=timeout)
    except TimeoutError:
        logger.warning("就绪探针超时 dependency=%s timeout=%ss", name, timeout)
        return ERROR
    except Exception as exc:  # noqa: BLE001
        # 只记一行，不带 traceback：探针周期性执行，依赖长时间不可用会刷爆日志，
        # 反而盖住真正的原因。
        logger.warning("就绪探针失败 dependency=%s error=%s", name, exc)
        return ERROR


@router.get("", response_model=ApiResponse[HealthResponse], summary="存活探针（liveness）")
async def liveness() -> ApiResponse[HealthResponse]:
    """只回答"进程活着、应用装配正常"，不查任何外部依赖。

    这里**故意**不碰数据库：liveness 失败会触发重启，
    如果它依赖 MySQL，那 MySQL 抖一下就会把所有实例一起重启 ——
    本来只是登录慢，结果变成全站不可用。
    """
    return ok(
        HealthResponse(
            status="ok",
            name=settings.PROJECT_NAME,
            version=settings.VERSION,
        )
    )


@router.get(
    "/ready",
    response_model=ApiResponse[ReadinessResponse],
    summary="就绪探针（readiness）",
)
async def readiness(
    response: Response,
    service: LLMService = Depends(get_llm_service),
    config: Settings = Depends(get_settings),
) -> ApiResponse[ReadinessResponse]:
    """检查对外能力是否可用。

    三档结论：
        ready      全部依赖正常
        degraded   有依赖**未配置**（功能关闭），仍返回 200 —— 不摘流量
        not_ready  有依赖**配了但连不上**，返回 503 —— 让负载均衡摘掉本实例

    为什么"未配置"不算不健康：
        本地开发常常只配一部分（比如没起 Redis），这时服务仍能提供对话能力，
        一刀切 503 会把"能跑一半"变成"完全跑不了"。

    为什么 DB/Redis 挂了必须 503：
        这两个是登录态的硬依赖。留着流量只会让用户看到一堆 500，
        不如让负载均衡把请求转给健康实例。
    """
    timeout = config.HEALTH_CHECK_TIMEOUT_SECONDS
    # 并发而不是串行：串行时最坏要等 3 倍超时，探针会被自己的超时干掉
    results = await asyncio.gather(
        _probe("db", lambda: _probe_db(config), timeout=timeout),
        _probe("redis", lambda: _probe_redis(config), timeout=timeout),
        _probe("llm", lambda: _probe_llm(service), timeout=timeout),
    )
    checks = dict(zip(("db", "redis", "llm"), results, strict=True))

    if ERROR in results:
        # 用注入的 Response 改状态码，同时换成失败信封。
        # 只改状态码是不够的：body 里 code 还是 0 的话，
        # 按 `code === 0` 判断成败的前端会把 503 当成功。
        response.status_code = ErrorCode.SERVICE_UNAVAILABLE.http_status
        return fail(
            ErrorCode.SERVICE_UNAVAILABLE,
            data=ReadinessResponse(status="not_ready", checks=checks),
        )

    status = "degraded" if NOT_CONFIGURED in results else "ready"
    return ok(ReadinessResponse(status=status, checks=checks))
