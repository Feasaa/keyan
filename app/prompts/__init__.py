"""提示词包：正文放 files/*.md，加载与校验在 loader.py，取用走 registry.py。"""

from functools import lru_cache

from app.prompts.loader import Prompt, PromptConfigError
from app.prompts.registry import PromptKey, PromptRegistry


@lru_cache(maxsize=1)
def get_prompt_registry() -> PromptRegistry:
    """提示词注册表：进程级单例，首次使用时加载并校验。"""
    return PromptRegistry()


def get_prompt(key: PromptKey | str) -> Prompt:
    return get_prompt_registry().get(key)


__all__ = [
    "Prompt",
    "PromptConfigError",
    "PromptKey",
    "PromptRegistry",
    "get_prompt",
    "get_prompt_registry",
]
