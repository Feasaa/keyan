"""PyMySQL / aiomysql 转义兼容补丁的回归测试。

为什么单独测这个：
    PyMySQL 1.2 把 `converters.escape_bytes_prefixed` 换成了字符串占位符
    （"DO NOT IMPORT THIS!!!"），只保证 import 不报错；aiomysql 0.3.2 仍然
    调用它，于是任何带 bytes 参数的 INSERT 都会在运行时抛
    `TypeError: 'str' object is not callable`。

    这个坑用 SQLite 跑的单元测试**永远发现不了** —— 只有真连 MySQL、
    真写一行带 bytes 列的数据才会炸。所以这里直接把"坏版本"的状态造出来，
    验证 `_patch_aiomysql_escape()` 能把它修回去。
"""

import pytest

pymysql_converters = pytest.importorskip("pymysql.converters")
aiomysql_connection = pytest.importorskip("aiomysql.connection")

from app.db.session import _patch_aiomysql_escape  # noqa: E402

# PyMySQL 1.2 里这两个名字的实际取值，抄过来当"坏版本"的形态
_BROKEN_PLACEHOLDER = "DO NOT IMPORT THIS!!!"


def test_patch_restores_callable_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    """坏版本下补丁要同时修好 converters 与 aiomysql 模块级引用。

    aiomysql 用的是 `from ... import escape_bytes_prefixed`，
    导入时就绑定了模块级名字，只改 pymysql 那边不够。
    """
    monkeypatch.setattr(
        pymysql_converters, "escape_bytes_prefixed", _BROKEN_PLACEHOLDER, raising=False
    )
    monkeypatch.setattr(
        aiomysql_connection, "escape_bytes_prefixed", _BROKEN_PLACEHOLDER, raising=False
    )

    _patch_aiomysql_escape()

    assert callable(pymysql_converters.escape_bytes_prefixed)
    assert callable(aiomysql_connection.escape_bytes_prefixed)


def test_patch_is_idempotent() -> None:
    """健康环境下不换实现，重复调用也不会反复包装。"""
    _patch_aiomysql_escape()
    first = pymysql_converters.escape_bytes_prefixed
    _patch_aiomysql_escape()
    assert pymysql_converters.escape_bytes_prefixed is first


def test_escape_handles_bytes() -> None:
    """补丁之后，bytes 参数能转成合法的 SQL 字面量（而不是抛 TypeError）。

    只测 bytes 分支：aiomysql 的 escape 走到 bytes 时不需要连接实例的任何状态，
    所以这里可以直接用未绑定方法 + 一个空壳对象。
    """
    _patch_aiomysql_escape()

    class _Stub:
        """bytes 分支只要不碰 self 的成员，空壳就够了。"""

    escaped = aiomysql_connection.Connection.escape(_Stub(), b"payload")

    assert isinstance(escaped, str)
    assert "payload" in escaped
