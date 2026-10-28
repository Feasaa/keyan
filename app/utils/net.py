"""IP 与二进制之间的转换。

`users.last_login_ip` / `auth_sessions.ip` 是 VARBINARY(16)，
存的语义等价于 MySQL 的 INET6_ATON：
    IPv4 → 4 字节，IPv6 → 16 字节
所以 INET6_NTOA(col) 能直接还原成字符串，DBA 手写 SQL 时不用猜格式。

不存字符串的原因：IPv6 最长 45 字符，配索引后既宽又慢；
而且同一个 IPv6 有压缩/展开多种写法，存字符串会出现"同一个地址多种写法"。
"""

import ipaddress


def pack_ip(ip: str | None) -> bytes | None:
    """字符串 → 字节。非法地址返回 None，不抛异常。

    为什么吞掉异常：这个值只用于审计，取不到就存 NULL，
    不该因为一个畸形 header 让整个登录流程 500。
    """
    if not ip:
        return None
    try:
        return ipaddress.ip_address(ip.strip()).packed
    except ValueError:
        return None


def unpack_ip(raw: bytes | None) -> str | None:
    """字节 → 字符串，用于回显与日志。"""
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None
