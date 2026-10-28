"""列类型：以 MySQL 的物理类型为准，其他方言下退化为通用类型。

为什么要包一层 `with_variant`：
    SQLAlchemy 的通用 `BigInteger` 只对应 `BIGINT`（有符号），
    而我们的主键是 `BIGINT UNSIGNED` —— 雪花 ID 是 63 位正数，
    有符号 BIGINT 存得下，但范围上限差一倍，且和 init/01-schema.sql 对不上。
    直接用 mysql.BIGINT 又会把模型绑死在 MySQL 上，
    连单元测试想用 SQLite 跑都跑不起来。

    `with_variant` 两者兼得：MySQL 上生成精确的物理类型，
    其他方言回落到通用类型。
"""

from enum import IntEnum, StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.dialects import mysql

# 雪花 ID：BIGINT UNSIGNED
SnowflakeID = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")

# BIGINT UNSIGNED 计数器：累计 token 这类只增不减的量。
# 物理类型和雪花 ID 相同，但语义不同，所以单独起名 ——
# 用 SnowflakeID 去标注一个计数器，读代码的人会以为那是主键。
UnsignedBigInt = BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")

# INT UNSIGNED：seq、token 计数、耗时
UnsignedInt = Integer().with_variant(mysql.INTEGER(unsigned=True), "mysql")

# TINYINT UNSIGNED：status 这类枚举字段
UnsignedTinyInt = SmallInteger().with_variant(mysql.TINYINT(unsigned=True), "mysql")

# MEDIUMTEXT：正文上限 16MB，比 TEXT（64KB）宽裕，比 LONGTEXT 克制
MediumText = Text().with_variant(mysql.MEDIUMTEXT(), "mysql")

# DATETIME(3)：毫秒精度。不带时区 —— 全链路约定存 UTC，
# 由应用侧写入，不依赖 MySQL 的时区转换。
Timestamp = DateTime().with_variant(mysql.DATETIME(fsp=3), "mysql")

# VARBINARY(16)：存 IP 用 INET6_ATON 的结果。
# 不用字符串：IPv6 最长 45 字符，存成字符串后索引又宽又慢。
# bytes 类型 SQLAlchemy 不会自动推断，必须显式给。
BinaryIP = LargeBinary(16).with_variant(mysql.VARBINARY(16), "mysql")


class StrEnumValue(TypeDecorator[StrEnum]):
    """StrEnum ↔ VARCHAR。

    数据库里仍然存字符串（配 CHECK 约束），但 ORM 层读写都是枚举成员。
    好处是写错值在**赋值时**就炸，而不是等到 commit 才收到
    MySQL 那句 `Check constraint 'ck_msg_role' is violated` ——
    那时业务逻辑已经跑完，栈也深得看不出是谁写错的。
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[StrEnum], length: int = 16) -> None:
        self._enum = enum_cls
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return None if value is None else self._enum(value).value

    def process_result_value(self, value: Any, dialect: Any) -> StrEnum | None:
        return None if value is None else self._enum(value)


class IntEnumValue(TypeDecorator[IntEnum]):
    """IntEnum ↔ TINYINT UNSIGNED。理由同上。"""

    impl = UnsignedTinyInt
    cache_ok = True

    def __init__(self, enum_cls: type[IntEnum]) -> None:
        self._enum = enum_cls
        super().__init__()

    def process_bind_param(self, value: Any, dialect: Any) -> int | None:
        return None if value is None else int(self._enum(value))

    def process_result_value(self, value: Any, dialect: Any) -> IntEnum | None:
        return None if value is None else self._enum(value)
