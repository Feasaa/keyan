"""鉴权接口的请求 / 响应契约。

密码策略
--------
只限制长度（8–128），**不要求大小写数字符号混排**。
这不是偷懒，是 NIST SP 800-63B 的明确建议：
    强制复杂度规则会把人逼向 "Passw0rd!" 这类可预测的变形，
    实际熵反而下降；而长度直接决定搜索空间。
配套要做的是"限制尝试次数"，而不是在字符集上做文章 ——
本项目两侧都做了：验证码 5 次/码，登录按账号 5 次、按 IP 20 次（15 分钟窗口），
见 app/services/login_throttle.py。

上限 128 是为了防 DoS：密码长度直接进哈希函数的计算量，
不设上限时，一个 1MB 的"密码"能让 argon2 占满一个核。
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


class VerificationScene(StrEnum):
    """验证码用途。

    它是 Redis key 的一部分，所以不同场景的验证码天然隔离 ——
    注册的码不能拿去重置密码。加新场景只需在这里加一个值。
    """

    REGISTER = "register"


class SendCodeRequest(BaseModel):
    email: EmailStr = Field(description="接收验证码的邮箱")
    scene: VerificationScene = Field(
        VerificationScene.REGISTER, description="验证码用途，目前仅注册"
    )


class RegisterRequest(BaseModel):
    email: EmailStr = Field(description="登录邮箱，需先获取验证码")
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH,
        max_length=PASSWORD_MAX_LENGTH,
        description=f"{PASSWORD_MIN_LENGTH}–{PASSWORD_MAX_LENGTH} 位，不限字符类型",
    )
    code: str = Field(pattern=r"^\d{6}$", description="6 位邮箱验证码")
    nickname: str = Field(
        "", max_length=64, description="展示名，留空则取邮箱 @ 前缀（见 user_repository.add）"
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        """统一小写。

        主流邮箱（QQ / Gmail / Outlook）的本地部分实际都不区分大小写，
        但数据库的 utf8mb4_0900_ai_ci 虽然不区分，Redis 的 key 区分 ——
        不归一化就会出现"用 Foo@qq.com 注册、用 foo@qq.com 登录查不到"。
        在入口处统一，比在每个查询处记得 lower() 可靠。
        """
        return value.strip().lower()


class LoginRequest(BaseModel):
    email: EmailStr = Field(description="注册邮箱")
    password: str = Field(
        max_length=PASSWORD_MAX_LENGTH, description="密码（登录不校验最小长度，避免提示策略）"
    )

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, description="注册/登录时拿到的 refresh_token")


class UserProfile(BaseModel):
    """对外暴露的用户信息。"""

    # 雪花 ID 必须序列化成字符串：
    # 19 位整数超过 JS 的 Number.MAX_SAFE_INTEGER（2^53-1，16 位），
    # 直接给数字，前端解析后末几位会被静默改写 —— 表现为"用户 ID 对不上"。
    id: str = Field(description="用户 ID（雪花 ID 字符串形式）")
    email: str
    nickname: str
    created_at: datetime


class TokenPair(BaseModel):
    """一次签发的一对令牌。"""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIs...",
                "refresh_token": "9tK3xQ...",
                "token_type": "Bearer",
                "expires_in": 900,
            }
        }
    )

    access_token: str = Field(description="放进 Authorization: Bearer <token>")
    refresh_token: str = Field(description="只用于换新 access_token，不要放进 URL")
    token_type: str = Field("Bearer", description="固定 Bearer")
    expires_in: int = Field(description="access_token 剩余有效秒数")


class AuthResult(BaseModel):
    """注册 / 登录 / 刷新的统一返回体。"""

    user: UserProfile
    token: TokenPair


class SendCodeResult(BaseModel):
    """发码结果。

    刻意**不返回验证码本身**，也不返回"该邮箱是否已注册" ——
    后者会让这个接口变成账号枚举工具。注册时的"邮箱已占用"
    只在真正提交注册时告知，那时用户已经证明了邮箱所有权。
    """

    email: str = Field(description="接收方邮箱（已归一化）")
    expires_in: int = Field(description="验证码有效秒数")
    resend_after: int = Field(description="多少秒后可以重新获取")
