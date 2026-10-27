"""提示词配置测试：默认值来自 app/prompts，且可被环境变量覆盖。"""

from app.core.config import Settings
from app.prompts import (
    ASSISTANT_PROMPT,
    PROMPTS,
    SYSTEM_PROMPT,
    PromptKey,
    compose_system_prompt,
    get_prompt,
)


def test_prompt_registry_lookup() -> None:
    assert get_prompt(PromptKey.SYSTEM) == SYSTEM_PROMPT
    assert get_prompt(PromptKey.ASSISTANT) == ASSISTANT_PROMPT
    assert set(PROMPTS) == {PromptKey.SYSTEM, PromptKey.ASSISTANT}


def test_settings_default_to_prompt_files() -> None:
    """不读 .env 时，配置默认值应与 prompts 包中的定义一致。"""
    settings = Settings(_env_file=None)

    assert settings.LLM_SYSTEM_PROMPT == SYSTEM_PROMPT
    assert settings.LLM_ASSISTANT_PROMPT == ASSISTANT_PROMPT


def test_compose_skips_empty_parts() -> None:
    assert compose_system_prompt("A", "", "B") == "A\n\nB"
    assert compose_system_prompt("", "  ") == ""
