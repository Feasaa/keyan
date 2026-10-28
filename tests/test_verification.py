"""邮箱验证码的签发与校验。

直连 Redis（测试库 db 15），不经 HTTP —— 这样测的是验证码本身的契约：
一次性、会过期、有冷却、有配额、存的是哈希。
接口层的错误映射由 test_auth.py 负责。
"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from app.core.config import Settings, get_settings
from app.services.exceptions import AuthRateLimitedError, VerificationCodeError
from app.services.verification_service import VerificationService
from redis.asyncio import Redis

SCENE = "register"
IP = "203.0.113.7"
# 配额用例单独占一个 IP：其它用例每发一次码都会累加 IP 计数，
# 共用同一个地址会让这条用例在跑到之前就已经超限。
QUOTA_IP = "198.51.100.42"


def _settings(**overrides: object) -> Settings:
    """测试配置：把 SMTP 字段填上，避免依赖开发机的真实发信配置。"""
    base: dict[str, object] = {
        "SMTP_USER": "noreply@example.com",
        "SMTP_PASSWORD": "test-only",
        "VERIFICATION_CODE_TTL_SECONDS": 300,
        "VERIFICATION_CODE_RESEND_SECONDS": 60,
        "VERIFICATION_CODE_MAX_ATTEMPTS": 5,
        "VERIFICATION_DAILY_LIMIT_PER_EMAIL": 10,
        "VERIFICATION_HOURLY_LIMIT_PER_IP": 20,
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


async def _run(
    redis_url: str,
    settings: Settings,
    stub: object,
    scenario: Callable[[VerificationService, Redis], Awaitable[None]],
) -> None:
    """在同一个事件循环里建客户端、跑场景、关客户端。

    跨 asyncio.run 复用同一个客户端会把连接绑在已关闭的 loop 上，
    症状是后面某个毫不相干的测试里冒出 Connection.__del__ 警告。
    """
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        await scenario(VerificationService(client, stub, settings), client)
    finally:
        await client.aclose()


async def _drop_cooldown(client: Redis, email: str) -> None:
    """跳过 60 秒冷却，用于测配额这类与冷却无关的行为。"""
    await client.delete(f"vc:cd:{SCENE}:{email}")


# ------------------------------------------------------------------ 基本流程


def test_stores_hash_not_plaintext(test_redis_url: str, stub_mail: object, make_email) -> None:
    """Redis 里存的必须是哈希。

    Redis 常被配成无密码、可远程访问，`KEYS vc:*` 就能 dump。
    存明文等于一次配置失误把所有在途验证码公开。
    """

    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        await service.issue(email=email, scene=SCENE, client_ip=IP)
        stored = await client.get(f"vc:code:{SCENE}:{email}")
        assert stored is not None
        assert stored != stub_mail.last_code
        assert len(stored) == 64  # sha256 十六进制

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


def test_issue_then_consume_is_one_time(test_redis_url: str, stub_mail: object, make_email) -> None:
    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        ttl, resend_after = await service.issue(email=email, scene=SCENE, client_ip=IP)
        assert ttl == 300
        assert resend_after == 60

        code = stub_mail.last_code
        await service.consume(email=email, scene=SCENE, code=code)

        # 用过的码立刻作废：同一个码不能用第二次
        with pytest.raises(VerificationCodeError):
            await service.consume(email=email, scene=SCENE, code=code)

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


def test_wrong_code_rejected(test_redis_url: str, stub_mail: object, make_email) -> None:
    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        await service.issue(email=email, scene=SCENE, client_ip=IP)
        wrong = "000000" if stub_mail.last_code != "000000" else "111111"
        with pytest.raises(VerificationCodeError):
            await service.consume(email=email, scene=SCENE, code=wrong)

        # 错一次不该把正确的码弄坏
        await service.consume(email=email, scene=SCENE, code=stub_mail.last_code)

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


def test_code_ttl_is_applied(test_redis_url: str, stub_mail: object, make_email) -> None:
    """验证码必须带 TTL。

    不带 TTL 的 key 会永久留在 Redis 里 —— 既占内存，
    也意味着"三天前发的码"理论上还能用。
    """

    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        await service.issue(email=email, scene=SCENE, client_ip=IP)
        ttl = await client.ttl(f"vc:code:{SCENE}:{email}")
        assert 0 < ttl <= 300

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


def test_code_is_bound_to_email_and_scene(
    test_redis_url: str, stub_mail: object, make_email
) -> None:
    """A 邮箱收到的码，不能拿去验证 B 邮箱。

    哈希里拌了 email 和 scene，所以即使两边的 key 都存在也对不上。
    """

    async def scenario(service: VerificationService, client: Redis) -> None:
        alice, bob = make_email(), make_email()
        await service.issue(email=alice, scene=SCENE, client_ip=IP)
        alice_code = stub_mail.last_code
        await _drop_cooldown(client, bob)
        await service.issue(email=bob, scene=SCENE, client_ip=IP)

        with pytest.raises(VerificationCodeError):
            await service.consume(email=bob, scene=SCENE, code=alice_code)

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


# ------------------------------------------------------------------ 限流


def test_resend_cooldown_blocks_second_request(
    test_redis_url: str, stub_mail: object, make_email
) -> None:
    """60 秒内重复发码要被拦住，且报错里带剩余秒数。

    不带剩余秒数的话，前端只能显示"操作太频繁"，用户不知道该等多久。
    """

    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        await service.issue(email=email, scene=SCENE, client_ip=IP)
        with pytest.raises(AuthRateLimitedError) as excinfo:
            await service.issue(email=email, scene=SCENE, client_ip=IP)
        assert "秒" in str(excinfo.value)

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


def test_attempts_exhaustion_locks_code(test_redis_url: str, stub_mail: object, make_email) -> None:
    """连错 5 次后，**正确的码也不能再用**。

    6 位数字只有 100 万种可能，5 分钟有效期内不限次数的话脚本能穷举完。
    这条测试锁的是"超限即作废"，不是"超限即报错"——
    只报错不作废，攻击者换个时间点还能接着试。
    """

    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        await service.issue(email=email, scene=SCENE, client_ip=IP)
        code = stub_mail.last_code
        wrong = "000000" if code != "000000" else "111111"

        for _ in range(5):
            with pytest.raises(VerificationCodeError):
                await service.consume(email=email, scene=SCENE, code=wrong)

        with pytest.raises(VerificationCodeError):
            await service.consume(email=email, scene=SCENE, code=code)

        assert await client.get(f"vc:code:{SCENE}:{email}") is None

    asyncio.run(_run(test_redis_url, _settings(), stub_mail, scenario))


def test_email_daily_quota(test_redis_url: str, stub_mail: object, make_email) -> None:
    """按邮箱的每日配额：换 IP 也绕不过去。"""

    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        for index in range(2):
            await _drop_cooldown(client, email)
            await service.issue(email=email, scene=SCENE, client_ip=f"{IP}.{index}")

        await _drop_cooldown(client, email)
        with pytest.raises(AuthRateLimitedError):
            await service.issue(email=email, scene=SCENE, client_ip=IP)

    asyncio.run(
        _run(
            test_redis_url,
            _settings(VERIFICATION_DAILY_LIMIT_PER_EMAIL=2),
            stub_mail,
            scenario,
        )
    )


def test_ip_hourly_quota(test_redis_url: str, stub_mail: object, make_email) -> None:
    """按 IP 的每小时配额：换邮箱也绕不过去。

    两层配额缺一不可 —— 只有邮箱维度，攻击者换邮箱就能刷；
    只有 IP 维度，分布式发包就能刷。
    """

    async def scenario(service: VerificationService, client: Redis) -> None:
        await service.issue(email=make_email(), scene=SCENE, client_ip=QUOTA_IP)
        with pytest.raises(AuthRateLimitedError):
            await service.issue(email=make_email(), scene=SCENE, client_ip=QUOTA_IP)

    asyncio.run(
        _run(
            test_redis_url,
            _settings(VERIFICATION_HOURLY_LIMIT_PER_IP=1),
            stub_mail,
            scenario,
        )
    )


def test_mail_failure_releases_cooldown(test_redis_url: str, make_email) -> None:
    """发信失败要把冷却也清掉。

    否则用户看到"发送失败"，想重试却被自己的 60 秒冷却挡住 ——
    明明一封邮件都没发出去。
    """

    class ExplodingMail:
        configured = True

        async def send_verification_code(self, **_: object) -> None:
            raise RuntimeError("smtp down")

    async def scenario(service: VerificationService, client: Redis) -> None:
        email = make_email()
        with pytest.raises(RuntimeError):
            await service.issue(email=email, scene=SCENE, client_ip=IP)

        assert await client.get(f"vc:code:{SCENE}:{email}") is None
        assert await client.get(f"vc:cd:{SCENE}:{email}") is None

        # 冷却已释放，换一个好用的邮件服务应当能立刻成功
        service._mail = _WorkingMail()  # noqa: SLF001
        await service.issue(email=email, scene=SCENE, client_ip=IP)

    class _WorkingMail:
        configured = True

        async def send_verification_code(self, **_: object) -> None:
            return None

    asyncio.run(_run(test_redis_url, _settings(), ExplodingMail(), scenario))
