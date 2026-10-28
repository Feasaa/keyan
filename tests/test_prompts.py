"""提示词测试：加载校验、热更新、以及提示词真的被注入到链里。"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from app.core.config import Settings
from app.prompts import PromptKey, get_prompt
from app.prompts.loader import PromptConfigError
from app.prompts.registry import PromptRegistry
from fastapi.testclient import TestClient

CHAT = "/api/v1/chat"


def write_prompt(
    directory: Path,
    *,
    name: str = "chat_system",
    version: int = 1,
    body: str = "正文内容",
    prompt_id: str | None = None,
) -> Path:
    path = directory / f"{name}.md"
    path.write_text(
        f"---\nid: {prompt_id or name}\nversion: {version}\ndescription: 测试用\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def touch_future(path: Path) -> None:
    """把 mtime 推到未来，确保注册表能感知到变化（避免文件系统时间精度问题）。"""
    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))


# ------------------------------------------------------------------ 内置提示词


def test_bundled_prompt_loads() -> None:
    prompt = get_prompt(PromptKey.CHAT_SYSTEM)

    assert prompt.id == "chat_system"
    assert prompt.version >= 1
    assert prompt.text
    assert len(prompt.checksum) == 12


def test_config_prompt_id_resolves() -> None:
    settings = Settings(_env_file=None)

    assert get_prompt(settings.PROMPT_CHAT_SYSTEM_ID).id == settings.PROMPT_CHAT_SYSTEM_ID


def test_prompt_text_is_injected_into_chain(
    client: TestClient, stub_llm: Callable[..., Any]
) -> None:
    """提示词从文件读出来，作为 system_prompt 传给链 —— 不是写死在链里。"""
    stub = stub_llm()

    client.post(CHAT, json={"message": "你好"})

    assert stub.calls[0]["system_prompt"] == get_prompt(PromptKey.CHAT_SYSTEM).text


# ------------------------------------------------------------------ 加载校验


def test_rejects_missing_front_matter(tmp_path: Path) -> None:
    (tmp_path / "chat_system.md").write_text("没有元数据的正文", encoding="utf-8")

    with pytest.raises(PromptConfigError, match="缺少 front matter"):
        PromptRegistry(tmp_path)


def test_rejects_id_mismatch(tmp_path: Path) -> None:
    write_prompt(tmp_path, name="chat_system", prompt_id="other")

    with pytest.raises(PromptConfigError, match="id 必须与文件名一致"):
        PromptRegistry(tmp_path)


def test_rejects_empty_body(tmp_path: Path) -> None:
    write_prompt(tmp_path, body="   ")

    with pytest.raises(PromptConfigError, match="正文为空"):
        PromptRegistry(tmp_path)


def test_rejects_non_numeric_version(tmp_path: Path) -> None:
    path = tmp_path / "chat_system.md"
    path.write_text("---\nid: chat_system\nversion: v2\n---\n正文\n", encoding="utf-8")

    with pytest.raises(PromptConfigError, match="version 必须是正整数"):
        PromptRegistry(tmp_path)


def test_rejects_empty_directory(tmp_path: Path) -> None:
    with pytest.raises(PromptConfigError, match="目录为空"):
        PromptRegistry(tmp_path)


def test_unknown_prompt_id_lists_available(tmp_path: Path) -> None:
    write_prompt(tmp_path)
    registry = PromptRegistry(tmp_path)

    with pytest.raises(PromptConfigError, match="未找到提示词"):
        registry.get("not_exists")


# ------------------------------------------------------------------ 热更新


def test_registry_reloads_when_file_changes(tmp_path: Path) -> None:
    write_prompt(tmp_path, version=1)
    registry = PromptRegistry(tmp_path)
    assert registry.get("chat_system").version == 1

    path = write_prompt(tmp_path, version=2, body="新正文")
    touch_future(path)

    prompt = registry.get("chat_system")
    assert prompt.version == 2
    assert prompt.text == "新正文"


def test_registry_keeps_old_version_when_reload_invalid(tmp_path: Path) -> None:
    """改坏了不能把线上打回"无提示词"状态，继续用旧版本。"""
    write_prompt(tmp_path, version=1)
    registry = PromptRegistry(tmp_path)

    path = tmp_path / "chat_system.md"
    path.write_text("改坏了，没有元数据", encoding="utf-8")
    touch_future(path)

    assert registry.get("chat_system").version == 1


def test_registry_picks_up_new_file(tmp_path: Path) -> None:
    write_prompt(tmp_path)
    registry = PromptRegistry(tmp_path)

    write_prompt(tmp_path, name="chat_other", version=1)

    assert registry.versions() == {"chat_system": 1, "chat_other": 1}
