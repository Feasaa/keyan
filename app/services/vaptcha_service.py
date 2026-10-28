"""VAPTCHA V4 人机验证：服务端二次验签。

前端（SDK 自动完成）
--------------------
加载 `https://c4.vaptcha.com/src/v4.js` → 用 VID 初始化 → 业务入口显式
`await vaptchaObj.validate()` → 拿到 `token / knock / dfu / ip` 四件套，
随业务请求原样提交。`ip` 是 **token 实际签名用的 IP 快照**，
服务端验签必须优先用它，不能自己换一个。

服务端（本模块）
----------------
两种方式，官方文档都给了完整算法，这里都实现：

1. `remote`（默认）—— POST `https://v41.vaptcha.com/api/verify`，
   JSON 提交 `vid/vkey/token/knock/dfu/ip`，读 `data.result + data.code`：
       data.code = 0 → 通过
       data.code = 1 → 鉴权不通过但 token 存在（IP 漂移常见，可宽松放行）
       data.code = 2 → 鉴权通过但 token 已过期
       data.code = 3 → 其它失败
   严格通过只有 `result=true, code=0`。

2. `local` —— 本地 HMAC-SHA256：
       token = "<时间戳>.<tokenId>.<签名>"（三段）
       期望签名 = hex(HMAC-SHA256(key=VKey, msg=f"{时间戳}.{ip}.{dfu}.{knock}"))
       时间窗口 = VAPTCHA_LOCAL_TTL_SECONDS
   少一次外网往返，代价是"票据在有效期内可被重放"——官方文档明确提示这点，
   所以本地模式额外用 Redis `SET NX` 把每张 token 标记为一次性。
   注意本地模式对 ip 极敏感：前端没回传 SDK 的签名 IP 就必然验签失败。

几条纪律
--------
- **VKey 只在服务端**。泄露 VKey 等于验签作废（任何人都能自己签给自己）。
- **立即验签**。票据带时间戳、有有效期，拖着不验就是给重放留窗口。
- **fail-closed**。验签上游超时 / 不可达 / 非 200 一律拒绝（504）。
  开着人机验证却在故障时放行 = 攻击者只要打挂验签链路就免验证。
"""

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import Settings
from app.schemas.auth import CaptchaPayload
from app.services.exceptions import CaptchaInvalidError, CaptchaUpstreamError

logger = logging.getLogger(__name__)

# 一次性票据标记的 key 前缀。只在 local 模式下写，TTL 与票据有效期对齐。
_USED_TOKEN_KEY = "vc:vt:{}"


def _issued_ago(token: str) -> str:
    """算出票据签发至今过了多久，只用于日志。

    token 第一段是秒级时间戳。解不出来就返回占位串 —— 诊断信息不能反过来
    把请求搞挂，那等于把"验签失败"升级成"500"。
    """
    head = token.split(".", 1)[0]
    try:
        return f"{time.time() - int(head):.0f}s"
    except ValueError:
        return "(不可解析)"


class VaptchaService:
    def __init__(
        self,
        settings: Settings,
        redis: Redis | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # Redis 仅 local 模式需要（一次性标记）。未配置时降级为"不做重放防护"
        self._redis = redis
        # transport 仅测试注入（httpx.MockTransport），生产恒为 None
        self._transport = transport

    @property
    def enabled(self) -> bool:
        """未配置 VID/VKey = 功能关闭，verify() 直接放行。"""
        return self._settings.vaptcha_configured

    @property
    def _local_mode(self) -> bool:
        return self._settings.VAPTCHA_VERIFY_MODE.strip().lower() == "local"

    async def verify(self, captcha: CaptchaPayload, *, client_ip: str | None) -> None:
        """验一张人机验证票据。通过即返回，不通过抛 CaptchaInvalidError。"""
        if not self.enabled:
            return

        # 配置了验签却没带票据：直接拒绝，不必浪费一次上游往返
        if not captcha.token or not captcha.knock:
            raise CaptchaInvalidError("缺少人机验证票据")

        # 优先用 SDK 的签名 IP：token 是拿那个 IP 签的，换一个必然对不上。
        # 前端没带时才退回服务端看到的来源地址（nginx/uvicorn 需正确透传才准）。
        ip = captcha.ip or client_ip or ""

        if self._local_mode:
            self._verify_local(captcha, ip=ip)
            await self._mark_used(captcha.token)
            logger.info("vaptcha verify passed mode=local")
            return

        await self._verify_remote(captcha, ip=ip)

    # ------------------------------------------------------------------ local

    def _verify_local(self, captcha: CaptchaPayload, *, ip: str) -> None:
        """本地 HMAC-SHA256 校验，算法照抄官方示例。

        token 三段：`时间戳.tokenId.签名`。tokenId 不参与签名，
        它的作用只是让同一个时间戳下的票据不重复。
        """
        parts = captcha.token.split(".")
        if len(parts) != 3:
            logger.info("vaptcha local: token 结构不合法")
            raise CaptchaInvalidError()

        timestamp, _token_id, signature = parts
        try:
            issued_at = int(timestamp)
        except ValueError:
            logger.info("vaptcha local: 时间戳不是整数")
            raise CaptchaInvalidError() from None

        ttl = self._settings.VAPTCHA_LOCAL_TTL_SECONDS
        if abs(int(time.time()) - issued_at) > ttl:
            logger.info("vaptcha local: 票据已过期 age=%.0fs ttl=%ss", time.time() - issued_at, ttl)
            raise CaptchaInvalidError("人机验证已过期，请重新验证")

        data = f"{timestamp}.{ip}.{captcha.dfu}.{captcha.knock}"
        expected = hmac.new(
            self._settings.VAPTCHA_VKEY.encode(), data.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.info("vaptcha local: 签名不匹配")
            raise CaptchaInvalidError()

    async def _mark_used(self, token: str) -> None:
        """把票据标记为已用，重复提交直接拒绝。

        `SET NX` 是原子的：并发提交同一张票据时只有一个能拿到标记，
        其余全部失败 —— 这正是"票据一次性"要的效果。
        Redis 没配时只告警不拦截：宁可有重放窗口，也不能让功能整体不可用。
        """
        if self._redis is None:
            logger.warning("vaptcha local: 未配置 Redis，票据重放检测已关闭")
            return

        key = _USED_TOKEN_KEY.format(hashlib.sha256(token.encode()).hexdigest()[:32])
        ttl = max(self._settings.VAPTCHA_LOCAL_TTL_SECONDS, 1)
        acquired = await self._redis.set(key, "1", nx=True, ex=ttl)
        if not acquired:
            logger.info("vaptcha local: 票据重复使用")
            raise CaptchaInvalidError("人机验证票据已被使用，请重新验证")

    # ----------------------------------------------------------------- remote

    async def _verify_remote(self, captcha: CaptchaPayload, *, ip: str) -> None:
        payload = {
            "vid": self._settings.VAPTCHA_VID,
            "vkey": self._settings.VAPTCHA_VKEY,
            "token": captcha.token,
            "knock": captcha.knock,
            "dfu": captcha.dfu,
            "ip": ip,
        }
        body = await self._post_verify(payload)

        # 外层 code 是"这次调用本身成不成功"（vid/vkey 错、限流等），
        # 与票据是否通过无关 —— 外层非 0 属于我们配置/依赖的问题，不是用户输错。
        if body.get("code") != 0:
            logger.warning(
                "vaptcha remote: 调用失败 code=%s msg=%s", body.get("code"), body.get("msg")
            )
            raise CaptchaUpstreamError(f"verify 返回 code={body.get('code')}")

        data: dict[str, Any] = body.get("data") or {}
        result = data.get("result") is True
        code = data.get("code")

        if result and code == 0:
            logger.info("vaptcha verify passed mode=remote")
            return

        # 宽松策略：token 存在但鉴权不通过，多见于政企老宽带 IP 漂移。
        # 默认关闭 —— 放行它就等于放宽了 IP 绑定，属于安全 vs 可用性的取舍。
        if not result and code == 1 and self._settings.VAPTCHA_ALLOW_CODE_1:
            logger.warning("vaptcha remote: code=1 宽松放行（已开启 VAPTCHA_ALLOW_CODE_1）")
            return

        # 失败时把可判定的线索一次打全，省得再回头猜：
        # token 只留结构信息（时间戳 / 段数），票据本身不打 —— 日志泄露等于送一张可重放的票。
        # 用 warning 而不是 info：验签失败本身值得被看见，且应用未单独配置 logging 时
        # info 级别会被 root 的 WARNING 挡掉，诊断信息等于没打。
        logger.warning(
            "vaptcha remote: 未通过 result=%s code=%s token_segments=%d issued_ago=%s knock_len=%d "
            "dfu_len=%d ip=%s resp_ts=%s",
            result,
            code,
            len(captcha.token.split(".")),
            _issued_ago(captcha.token),
            len(captcha.knock or ""),
            len(captcha.dfu or ""),
            ip or "(空)",
            data.get("timestamp"),
        )

        # code=2 明确是「验过了但票过期」，文案要能引导用户直接重验，
        # 不能和"验签不通过"混在一起让用户以为自己操作错了。
        if code == 2:
            raise CaptchaInvalidError("人机验证已过期，请重新验证")
        raise CaptchaInvalidError()

    async def _post_verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调官方 verify 接口并解析 JSON。

        所有上游故障（超时 / DNS / 非 200 / 非法 JSON）都归一为
        CaptchaUpstreamError，原始异常挂 __cause__ 供日志溯源。
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.VAPTCHA_TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                # V4 是 JSON 提交（V2 才是 form 的 id/secretkey/scene）
                resp = await client.post(self._settings.VAPTCHA_VERIFY_URL, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("vaptcha verify unreachable: %r", exc)
            raise CaptchaUpstreamError("verify 接口不可达") from exc

        if resp.status_code != 200:
            logger.warning("vaptcha verify http %s", resp.status_code)
            raise CaptchaUpstreamError(f"verify 返回 HTTP {resp.status_code}")

        try:
            return resp.json()
        except ValueError as exc:
            logger.warning("vaptcha verify bad json: %s", resp.text[:200])
            raise CaptchaUpstreamError("verify 返回格式异常") from exc
