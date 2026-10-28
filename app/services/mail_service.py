"""SMTP 发信（QQ 邮箱 / 任意标准 SMTP）。

几个不显眼但会出事的地方
------------------------
1. **每次发信新建连接**。验证码是低频操作（每用户每分钟最多一次），
   长连接带来的收益接近于零，而代价是必须处理"连接被服务端空闲断开"。
   SMTP 服务端普遍有 30–60 秒的 idle 超时，复用一个放着不动的连接，
   下一个请求就会收到 `SMTPServerDisconnected`，且只在低峰期出现。

2. **发件人必须与登录账号一致**。QQ / 163 等都会校验 From 与认证用户，
   伪造 From 会被拒（501/553）。所以 From 直接取 SMTP_USER，不开放配置。

3. **头注入**。收件人来自 EmailStr 校验，email-validator 会拒绝含
   CRLF 的地址；主题是常量。这两点合起来堵住了"往 Subject 里塞
   Bcc: 全公司"这类 SMTP 头注入。不要把用户可控字符串拼进任何 header。

4. **日志里不出现验证码**。日志的访问面比数据库宽，
   验证码写进日志等于把"邮箱所有权证明"公开。
"""

import logging
from email.message import EmailMessage
from email.utils import formataddr, formatdate

import aiosmtplib

from app.core.config import Settings, get_settings
from app.services.exceptions import AuthNotConfiguredError, MailSendError
from app.utils.masking import mask_email

logger = logging.getLogger(__name__)

SCENE_LABELS = {
    "register": "注册",
}


class MailService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.configured = settings.mail_configured

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise AuthNotConfiguredError("SMTP_USER / SMTP_PASSWORD")

    async def send_verification_code(
        self, *, to: str, code: str, scene: str, ttl_seconds: int
    ) -> None:
        self._ensure_configured()
        label = SCENE_LABELS.get(scene, "验证")
        message = self._build_message(to=to, code=code, label=label, ttl_seconds=ttl_seconds)
        await self._send(message, recipient=to)

    # ------------------------------------------------------------------ 内部

    def _build_message(self, *, to: str, code: str, label: str, ttl_seconds: int) -> EmailMessage:
        settings = self._settings
        minutes = max(1, ttl_seconds // 60)

        message = EmailMessage()
        # From 用认证账号，不能用别名的原因见模块 docstring 第 2 条
        message["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_USER))
        message["To"] = to
        message["Subject"] = f"[{settings.PROJECT_NAME}] {label}验证码 {code}"
        message["Date"] = formatdate(localtime=True)

        text = (
            f"您好：\n\n"
            f"您的{label}验证码是：{code}\n\n"
            f"该验证码 {minutes} 分钟内有效，只能使用一次。\n"
            f"请勿转发给他人 —— 拿到验证码的人可以完成{label}。\n\n"
            f"如果这不是您本人的操作，请直接忽略本邮件。\n"
        )
        message.set_content(text)

        # 加一份 HTML：纯文本邮件在 QQ / 微信里排版会塌，
        # 加 HTML 后客户端自动择优显示，纯文本仍作为降级保留。
        message.add_alternative(
            f"""\
<html><body style="font-family:-apple-system,'PingFang SC',sans-serif;line-height:1.7">
  <p>您好：</p>
  <p>您的{label}验证码是：</p>
  <p style="font-size:28px;font-weight:700;letter-spacing:6px;margin:16px 0">{code}</p>
  <p style="color:#666">该验证码 {minutes} 分钟内有效，只能使用一次。<br>
     请勿转发给他人 —— 拿到验证码的人可以完成{label}。</p>
  <p style="color:#999;font-size:13px">如果这不是您本人的操作，请直接忽略本邮件。</p>
</body></html>""",
            subtype="html",
        )
        return message

    async def _send(self, message: EmailMessage, *, recipient: str) -> None:
        settings = self._settings
        try:
            await aiosmtplib.send(
                message,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USER,
                password=settings.SMTP_PASSWORD,
                # 465 = 隐式 SSL（连上即加密）；587 = 明文连接后 STARTTLS 升级。
                # 两个参数互斥，同时为 True 会直接抛异常。
                use_tls=settings.smtp_use_ssl,
                start_tls=not settings.smtp_use_ssl,
                timeout=settings.SMTP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            # 原始异常（可能带账号、服务器地址）只进日志，不进响应
            logger.exception(
                "smtp send failed host=%s port=%s to=%s",
                settings.SMTP_HOST,
                settings.SMTP_PORT,
                mask_email(recipient),
            )
            raise MailSendError(type(exc).__name__) from exc

        logger.info(
            "smtp sent to=%s host=%s port=%s",
            mask_email(recipient),
            settings.SMTP_HOST,
            settings.SMTP_PORT,
        )


def get_mail_service() -> MailService:
    """工厂。没有 @lru_cache：MailService 无状态、构造成本可忽略，
    而 SMTP 连接是每封信新建的，没有需要复用的资源。"""
    return MailService(get_settings())
