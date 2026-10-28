"""鉴权接口。

流程（注册）：
    POST /auth/verification-code   → 给邮箱发 6 位码（Redis 存哈希，5 分钟）
    POST /auth/register            → 邮箱 + 密码 + 码，验过即建号并直接返回令牌

流程（登录）：
    POST /auth/login               → 邮箱 + 密码
    POST /auth/refresh             → refresh_token 换新的一对（旧 token 立刻作废）
    POST /auth/logout              → 撤销 refresh_token
    GET  /auth/me                  → 验证 access_token 是否有效

为什么发码是同步的（而不是丢后台任务）：
    发信失败必须让用户当场知道 —— 否则用户看到"已发送"，等 5 分钟收不到，
    再点重发被 60 秒冷却挡住，体验直接崩。
    aiosmtplib 是异步的，等待期间不占线程，单次 1–2 秒可以接受。
"""

from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.api.deps import CurrentUser, client_ip, get_auth_service, user_agent
from app.core.response import ApiResponse, ok
from app.db.models import User
from app.schemas.auth import (
    AuthResult,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SendCodeRequest,
    SendCodeResult,
    TokenPair,
    UserProfile,
)
from app.services.auth_service import AuthService, IssuedTokens

router = APIRouter()


def _profile(user: User) -> UserProfile:
    """User → 对外结构。

    时间统一带 UTC 时区再序列化：库里存的是 naive UTC（见 app/db/base.py），
    直接吐出去会是 `2026-10-28T04:00:00` 这种没有时区的字符串，
    前端 new Date() 会按**浏览器本地时区**解析，直接错 8 小时。
    加上 tzinfo 后输出带 `Z` 后缀的 UTC 时间，前端转本地时间才是对的。
    """
    return UserProfile(
        id=str(user.id),
        email=user.email or "",
        nickname=user.nickname,
        created_at=user.created_at.replace(tzinfo=UTC),
    )


def _token_pair(tokens: IssuedTokens) -> TokenPair:
    return TokenPair(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_in=tokens.expires_in,
    )


@router.post(
    "/verification-code",
    response_model=ApiResponse[SendCodeResult],
    summary="发送邮箱验证码",
)
async def send_verification_code(
    payload: SendCodeRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[SendCodeResult]:
    """给邮箱发 6 位验证码。

    不返回验证码本身，也不返回"该邮箱是否已注册" ——
    后者会让这个接口变成账号枚举工具。
    """
    expires_in, resend_after = await service.send_register_code(
        email=payload.email,
        scene=payload.scene,
        client_ip=client_ip(request),
    )
    return ok(
        SendCodeResult(
            email=payload.email,
            expires_in=expires_in,
            resend_after=resend_after,
        ),
        message="验证码已发送，请查收邮件",
    )


@router.post(
    "/register",
    response_model=ApiResponse[AuthResult],
    status_code=status.HTTP_201_CREATED,
    summary="邮箱注册",
)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[AuthResult]:
    """注册成功直接返回令牌，省掉"注册完再登一次"的多余往返。"""
    user, tokens = await service.register(
        email=payload.email,
        password=payload.password,
        code=payload.code,
        nickname=payload.nickname,
        client_ip=client_ip(request),
        user_agent=user_agent(request),
    )
    return ok(AuthResult(user=_profile(user), token=_token_pair(tokens)), message="注册成功")


@router.post("/login", response_model=ApiResponse[AuthResult], summary="邮箱密码登录")
async def login(
    payload: LoginRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[AuthResult]:
    """邮箱 + 密码登录。

    密码错误与邮箱不存在返回**同一个**错误码和文案，避免账号枚举。
    """
    user, tokens = await service.login(
        email=payload.email,
        password=payload.password,
        client_ip=client_ip(request),
        user_agent=user_agent(request),
    )
    return ok(AuthResult(user=_profile(user), token=_token_pair(tokens)), message="登录成功")


@router.post("/refresh", response_model=ApiResponse[AuthResult], summary="刷新令牌")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[AuthResult]:
    """用 refresh_token 换一对新令牌，旧的立刻失效（轮换）。"""
    user, tokens = await service.refresh(
        refresh_token=payload.refresh_token,
        client_ip=client_ip(request),
        user_agent=user_agent(request),
    )
    return ok(AuthResult(user=_profile(user), token=_token_pair(tokens)))


@router.post("/logout", response_model=ApiResponse[None], summary="登出")
async def logout(
    payload: RefreshRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[None]:
    """撤销 refresh_token。幂等：不存在也返回成功。

    注意 access_token 在剩余有效期（默认 15 分钟）内仍然可用 ——
    这是 JWT 自包含的固有代价，换来的是每个接口不必查一次会话表。
    """
    await service.logout(refresh_token=payload.refresh_token)
    return ok(message="已登出")


@router.get("/me", response_model=ApiResponse[UserProfile], summary="当前登录用户")
async def me(user: CurrentUser) -> ApiResponse[UserProfile]:
    """拿 access_token 换当前用户信息，前端可用它校验登录态是否还有效。"""
    return ok(_profile(user))
