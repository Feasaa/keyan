"""提示词注册表：按名字取提示词，便于配置化与后续扩展。

新增提示词只需在 PromptKey 和 PROMPTS 各加一行，
调用方通过 get_prompt(PromptKey.XXX) 获取，不直接 import 具体模块。
"""

from enum import StrEnum

from app.prompts.assistant import ASSISTANT_PROMPT
from app.prompts.system import SYSTEM_PROMPT


class PromptKey(StrEnum):
    SYSTEM = "system"
    ASSISTANT = "assistant"


PROMPTS: dict[PromptKey, str] = {
    PromptKey.SYSTEM: SYSTEM_PROMPT,
    PromptKey.ASSISTANT: ASSISTANT_PROMPT,
}


def get_prompt(key: PromptKey) -> str:
    """按名字取提示词；未知名字直接 KeyError，避免静默用错提示词。"""
    return PROMPTS[key]


def compose_system_prompt(*parts: str) -> str:
    """按顺序拼接多段提示词，空段自动跳过。"""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())
