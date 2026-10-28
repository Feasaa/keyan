"""防 SQL 注入：静态扫描 + 运行时载荷。

分两层，因为这是两种完全不同的失败模式：

1. **静态扫描**（不需要数据库）—— 防"将来有人写了一句 text(f"...")"。
   这类问题 code review 靠不住：它长得和正常代码几乎一样，
   而且写的人往往觉得"这是内部变量，安全"。

2. **运行时载荷**（需要数据库）—— 防"参数绑定本身没生效"。
   比如误用了 `literal_column`、或者某处 `compile_kwargs={"literal_binds": True}`。

另外单独验证一遍"ORM 到底编译成了什么"：
这是最直接的证据，比读文档有说服力。
"""

import ast
import asyncio
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.db.models import User
from sqlalchemy import select, text
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# 会把参数当 SQL 用的函数。参数一旦是运行时拼出来的字符串，就是注入面。
SQL_SINKS = {
    "text",
    "execute",
    "exec_driver_sql",
    "from_statement",
    "literal_column",
}

# 经典载荷。有的会被参数校验挡住，有的会走到 SQL 层 —— 两种都必须安全。
PAYLOADS = [
    "' OR '1'='1",
    "admin'--",
    "' OR 1=1 --",
    '" OR ""="',
    "'; DROP TABLE users; --",
    "1' UNION SELECT NULL, NULL, NULL --",
    "\\' OR 1=1 #",
]


# ------------------------------------------------------------------ 静态扫描


def _dynamic_sql_calls(source: str, filename: str) -> list[str]:
    """找出"把动态字符串交给 SQL 函数"的调用点。

    用 AST 而不是正则：正则会把注释和 docstring 里的示例也算进去
    （本项目的仓储层 docstring 里就写着 `text(f"...")` 作为反例），
    结果是测试永远红着，最后被人加 skip 跳过 —— 比没有测试更糟。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - 语法错由 ruff/pytest 先报
        return []

    problems: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name not in SQL_SINKS:
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):  # f-string
                problems.append(f'{filename}:{node.lineno} {name}(f"...")')
            elif isinstance(arg, ast.BinOp) and isinstance(arg.op, (ast.Add, ast.Mod)):
                problems.append(f"{filename}:{node.lineno} {name}(... + / % ...)")
            elif (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "format"
            ):
                problems.append(f"{filename}:{node.lineno} {name}(....format(...))")
    return problems


def test_no_dynamic_sql_construction() -> None:
    """源码里不允许把变量拼进 SQL 文本。

    允许的写法只有一种：
        text("... WHERE email = :email")  然后传参数字典
    占位符由驱动负责转义，值走协议单独传，物理上不参与 SQL 解析。
    """
    problems: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        problems.extend(_dynamic_sql_calls(path.read_text(encoding="utf-8"), str(path)))

    assert not problems, "发现动态拼接 SQL（会注入），改用绑定参数：\n" + "\n".join(problems)


def test_orm_query_compiles_to_bind_parameter() -> None:
    """直接看编译结果：载荷不会出现在 SQL 文本里。

    这比"相信 ORM 会参数化"可靠 —— 一旦有人给 compile 加了
    literal_binds=True，SQL 文本里就会出现载荷原文，这条测试立刻红。
    """
    payload = PAYLOADS[0]
    statement = select(User).where(User.email == payload)
    compiled = str(statement.compile(dialect=mysql.dialect()))

    assert payload not in compiled, f"载荷出现在 SQL 文本里：{compiled}"
    # aiomysql/pymysql 走的是位置占位符 %s（不是 psycopg 的 %(name)s）。
    # 只要语句里是占位符而不是载荷原文，值就是走协议单独传的。
    assert "%s" in compiled or "%(" in compiled, f"没有绑定参数占位符：{compiled}"


# ------------------------------------------------------------------ 运行时载荷


async def _user_count() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            return int(await conn.scalar(text("SELECT COUNT(*) FROM users")) or 0)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("payload", PAYLOADS)
def test_login_payload_cannot_bypass(auth_client, payload: str) -> None:
    """登录接口收到注入串必须 401，不能 200、也不能 500。

    200 = 被绕过了；500 = 载荷进了 SQL 并让它语法错误 ——
    后者同样是注入成功的证据（说明值被当成了代码）。
    """
    response = auth_client.post(
        "/api/v1/auth/login", json={"email": payload, "password": "whatever"}
    )
    # 邮箱字段有格式校验，绝大多数载荷在进入 SQL 之前就被 422 挡下
    assert response.status_code in (401, 422), response.text
    assert response.json()["code"] in (11401, 10422)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_password_payload_is_just_a_string(auth_client, make_email, payload: str) -> None:
    """密码字段没有格式限制，载荷会一路走到哈希比对 —— 那里必须安全。

    密码只参与哈希计算，永远不进 SQL，所以无论多奇怪的字符串，
    结果都只能是"密码不正确"。

    每个载荷用一个独立邮箱：登录失败是按账号计数的，共用一个邮箱会在
    第 6 个载荷时被限流成 429，掩盖掉真正要断言的 401。
    """
    response = auth_client.post(
        "/api/v1/auth/login", json={"email": make_email(), "password": payload}
    )
    assert response.status_code == 401, response.text
    assert response.json()["code"] == 11401


def test_payloads_do_not_damage_database(auth_client) -> None:
    """跑完所有载荷，users 表还在、行数没变。

    `'; DROP TABLE users; --` 这类载荷如果真被执行，后果是表没了。
    这里连"表是否还存在"一起验证 —— 查询本身能跑通就说明表还在。
    """
    settings = get_settings()
    if not settings.db_configured:
        pytest.skip("未配置 DB_PASSWORD")

    try:
        before = asyncio.run(_user_count())
    except (SQLAlchemyError, OSError) as exc:
        pytest.skip(f"数据库不可达：{type(exc).__name__}")

    for payload in PAYLOADS:
        auth_client.post("/api/v1/auth/login", json={"email": payload, "password": payload})
        auth_client.post("/api/v1/auth/verification-code", json={"email": payload})

    after = asyncio.run(_user_count())
    assert after == before, "注入载荷改变了 users 表的行数"


def test_register_payload_rejected_by_validation(auth_client) -> None:
    """注册接口的 code 字段有 `^\\d{6}$` 约束，注入串在参数校验就被挡下。"""
    response = auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "someone@example.com",
            "password": "correct horse battery staple",
            "code": "' OR 1=1 --",
        },
    )
    assert response.status_code == 422
