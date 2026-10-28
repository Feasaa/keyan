"""路径参数里的雪花 ID 解析。

为什么要在边界上转换，而不是让 FastAPI 直接声明 `int`：
    雪花 ID 是 19 位无符号整数，前台永远以**字符串**形式传（JS 的 Number
    装不下 19 位精度）。如果路径参数写成 `int`，FastAPI 会接受 "00123"
    和 "1e5" 这类形态，也会把前导零静默吃掉 —— 同一个会话可能出现两个
    看起来不同的路径却指向同一条记录，日志和缓存键都会变脏。
    这里只接受纯数字字符串，其余一律 400。
"""

from app.core.errors import InvalidParamError


def parse_snowflake(value: str, *, field: str = "ID") -> int:
    """把路径里的 ID 字符串转成整数，失败抛 400。

    不抛 ValueError 而是 InvalidParamError：让非法 ID 走统一的错误响应，
    而不是变成 500 或者 FastAPI 默认的 422 结构（前端要分别处理两套格式）。
    """
    text = value.strip()
    if text.isdigit():
        parsed = int(text)
        # 0 不是合法的雪花 ID（生成器从 1 开始），但 "0" 能通过 isdigit，
        # 放进去只会查出一片空，不如在边界上直接拒掉
        if parsed > 0:
            return parsed
    raise InvalidParamError(f"{field} 格式不正确")
