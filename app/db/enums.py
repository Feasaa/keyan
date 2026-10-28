"""状态枚举：与 init/01-schema.sql 里的 CHECK 约束一一对应。

改这里的值，必须同步改 CHECK 约束，否则写入会被数据库拒绝。
值一旦上线就不能改 —— 历史数据里存的是这些数字。
"""

from enum import IntEnum, StrEnum


class UserStatus(IntEnum):
    DISABLED = 0
    ACTIVE = 1
    LOCKED = 2


class ConversationStatus(IntEnum):
    ACTIVE = 0
    ARCHIVED = 1


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    # system 只由服务端注入，不接受客户端传入（见 schemas/chat.py 的 role 白名单）
    SYSTEM = "system"
    TOOL = "tool"


class MessageStatus(IntEnum):
    """流式写入的状态机，也是 Redis 与 MySQL 的接缝。

    STREAMING   --流结束-->  COMPLETE
        |                       ^
        |--客户端断开-->  INTERRUPTED
        |--上游报错---->  FAILED

    停在 STREAMING 的记录说明进程中途死了，
    由对账任务捞出来标成 INTERRUPTED —— 所以这个状态不能省。
    """

    STREAMING = 0
    COMPLETE = 1
    INTERRUPTED = 2
    FAILED = 3


class MessageFeedback(IntEnum):
    """用户对单条回复的手动评价。

    NONE 不是"中立"，而是"没评价过"。必须区分这两件事：
    统计满意度时分母只能是"评价过的条数"，把没表态的算成中立，
    好评率会被稀释成一个永远接近 50% 的假数字。

    赞/踩之外不设中间档：五个档位的评价在 UI 上没人用，
    在数据上又没法解释 —— 要么有用要么没用，要么说不清就不要。
    """

    NONE = 0
    UP = 1
    DOWN = 2
