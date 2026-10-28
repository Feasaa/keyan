"""会话表的读写。

两条贯穿全文件的规则：

1. **归属条件写进 WHERE**，不是取出来再 `if conv.user_id != user.id`。
   后者漏一次判断就是越权读别人对话，而漏了不会有任何报错。
2. **软删除过滤也写进 WHERE**，理由同上：漏加一次，已删除的会话就重新出现在列表里。

分页用游标而不是 OFFSET：会话按 `last_message_at` 倒序，聊过的会话会不断
跳到列表最前，OFFSET 分页在这种"排序键会变"的列表上必然漏行或重复行。
"""

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.snowflake import get_snowflake
from app.db.base import utcnow
from app.db.enums import ConversationStatus
from app.db.models import Conversation

# 游标 = (上一页最后一条的 last_message_at, id)。
# 只用时间戳不够：同一毫秒建的会话时间戳相同，翻页会卡住或跳行。
Cursor = tuple[datetime, int]


class ConversationRepository:
    """一个实例绑定一个 AsyncSession，生命周期跟随会话（请求或一次后台写）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(
        self,
        *,
        user_id: int,
        title: str,
        model: str,
        prompt_id: str,
        prompt_version: int,
    ) -> Conversation:
        """构造并挂入会话，由调用方决定何时 flush / commit。

        ID 在应用侧生成（雪花），不等数据库自增：流式对话要在写库之前
        就把 conversation_id 返回给客户端，自增 ID 那时还没有。
        """
        conversation = Conversation(
            id=get_snowflake().next_id(),
            user_id=user_id,
            title=title,
            model=model,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            status=ConversationStatus.ACTIVE,
        )
        self._session.add(conversation)
        return conversation

    async def get(
        self,
        *,
        user_id: int,
        conversation_id: int,
        for_update: bool = False,
    ) -> Conversation | None:
        """按 (归属用户, ID) 取会话。

        `for_update=True` 会在事务结束前锁住这一行。分配消息 seq 时必须加锁：
        同一会话两条消息并发插入时，`MAX(seq)+1` 会算出同一个值，
        撞上 uk_msg_conv_seq 后整个请求失败。锁会话行等于把同一会话的写入串行化，
        不同会话之间互不影响。
        """
        stmt = select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self._session.scalar(stmt)

    async def list_page(
        self,
        *,
        user_id: int,
        limit: int,
        before: Cursor | None = None,
    ) -> list[Conversation]:
        """按最近活跃倒序取一页。一次多取一条由调用方判断还有没有下一页。"""
        stmt = select(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.deleted_at.is_(None),
        )
        if before is not None:
            last_at, last_id = before
            # 复合游标：时间更早的，或时间相同但 id 更小的
            stmt = stmt.where(
                or_(
                    Conversation.last_message_at < last_at,
                    and_(
                        Conversation.last_message_at == last_at,
                        Conversation.id < last_id,
                    ),
                )
            )
        stmt = stmt.order_by(Conversation.last_message_at.desc(), Conversation.id.desc()).limit(
            limit
        )
        return list((await self._session.scalars(stmt)).all())

    def touch(
        self,
        conversation: Conversation,
        *,
        new_messages: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """更新列表排序键与冗余统计。

        计数器用 `+=` 而不是回表 COUNT/SUM：列表页要显示"共 N 条"，
        每次翻页都去 COUNT 一遍消息表，会话一多就是灾难。
        代价是计数器可能和真实行数漂移（比如事务回滚），
        所以这是"可修复的展示值"，不能拿它做计费依据。
        """
        conversation.message_count += new_messages
        conversation.total_prompt_tokens += prompt_tokens
        conversation.total_completion_tokens += completion_tokens
        conversation.last_message_at = utcnow()

    def rename(self, conversation: Conversation, title: str) -> None:
        conversation.title = title

    def soft_delete(self, conversation: Conversation) -> None:
        """软删除：保留消息记录，只是从列表里消失。

        物理删除会连带删掉消息（fk 是 RESTRICT，删不掉还得先删子表），
        而对话内容是排查"模型答歪了"的唯一证据，删了就没法复盘。
        """
        conversation.deleted_at = utcnow()
