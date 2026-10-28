"""消息级操作：赞 / 踩。

为什么评价挂在消息上而不是会话上：
    用户真正想评价的是"这一条回答好不好"。会话级评价在长对话里没法解释 ——
    前 10 轮都很好、第 11 轮胡说，用户点个踩，整段会话就被标成差的了。
    消息级评价还能直接喂给评测集：把 down 的问答捞出来就是现成的 badcase。

一个端点同时承担"评价"和"取消评价"：`rating=null` 就是取消。
不额外开 DELETE 端点，是因为前端点第二次赞时最自然的动作就是
"再发一次同样的请求，值变成 null"，两套端点反而要在前端维护状态机。
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_conversation_service
from app.core.response import ApiResponse, ok
from app.db.enums import MessageFeedback
from app.db.models import Message
from app.schemas.conversation import FeedbackRequest, FeedbackResponse
from app.services.conversation_service import ConversationService
from app.utils.ids import parse_snowflake

router = APIRouter()

_FEEDBACK_NAMES = {MessageFeedback.UP: "up", MessageFeedback.DOWN: "down"}


@router.post(
    "/{message_id}/feedback",
    response_model=ApiResponse[FeedbackResponse],
    summary="评价助手回复（赞 / 踩 / 取消）",
    description=(
        "rating=up 点赞，rating=down 点踩，rating=null 取消之前的评价。"
        "只能评价 assistant 消息，且该消息必须属于当前用户。"
    ),
)
async def set_feedback(
    message_id: str,
    payload: FeedbackRequest,
    user: CurrentUser,
    service: Annotated[ConversationService, Depends(get_conversation_service)],
) -> ApiResponse[FeedbackResponse]:
    message = await service.set_feedback(
        user_id=user.id,
        message_id=parse_snowflake(message_id, field="message_id"),
        rating=payload.rating,
        reason=payload.reason,
    )
    return ok(_to_response(message), message=_hint(payload.rating))


def _to_response(message: Message) -> FeedbackResponse:
    return FeedbackResponse(
        message_id=str(message.id),
        rating=_FEEDBACK_NAMES.get(message.feedback),
        feedback_reason=message.feedback_reason,
        feedback_at=message.feedback_at,
    )


def _hint(rating: str | None) -> str:
    if rating == "up":
        return "已点赞"
    if rating == "down":
        return "已记录，感谢反馈"
    return "已取消评价"
