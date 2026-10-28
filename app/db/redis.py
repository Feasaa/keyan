"""Redis 客户端：验证码、限流计数。

和数据库引擎一样，**进程级单例**：redis-py 内部维护连接池，
每请求新建一个客户端就是每请求一个连接池，很快会把 Redis 的
maxclients（默认 10000）打满，而且报错是 "max number of clients reached"，
看起来像"Redis 挂了"，实际是自己泄漏。

decode_responses=True：验证码、计数器都是文本，
不开启的话每次都要自己 .decode()，漏一次就会拿 bytes 去比较字符串，
结果是"验证码永远不对"这种极难查的 bug。
"""

from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    settings = get_settings()
    if not settings.redis_configured:
        raise RuntimeError("Redis 未配置：请在 .env 里设置 REDIS_PASSWORD")

    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        # 连接池里躺久的连接可能已被服务端/中间网络设备断开，
        # 定时探活让坏连接在取用时就被剔除，而不是在业务调用时才炸
        health_check_interval=30,
    )


async def close_redis() -> None:
    """停机时释放连接池，由 lifespan 调用。"""
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
    get_redis.cache_clear()
