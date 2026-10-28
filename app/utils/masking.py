"""脱敏：写日志、回显时用。

日志里出现明文邮箱和令牌是最常见的隐私泄漏途径 ——
日志往往被采集到第三方平台、保留 30 天以上、访问权限比数据库宽得多。
所以"能不能打日志"和"能不能存库"是两个标准，日志要更严。
"""


def mask_email(email: str) -> str:
    """`zhangsan@example.com` → `zh***@example.com`。

    保留首尾是为了排障时还能对上是谁（同域名下前几位足够区分），
    中间的星号保证即使日志外泄也无法直接用于撞库或发信。
    """
    if not email or "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        return f"***@{domain}"
    keep = min(4, max(1, len(local) // 3))
    return f"{local[:keep]}***@{domain}"


def mask_token(token: str) -> str:
    """令牌只留前 6 位，够在日志里对上一次请求，但不足以冒用。"""
    if not token:
        return "***"
    return f"{token[:6]}***"
