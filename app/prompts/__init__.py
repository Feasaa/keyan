"""提示词包：加载与校验在 loader.py，取用走 registry.py。

正文不在这个包里 —— 它是内容而不是代码，放在仓库根的 prompts/ 目录，
位置由配置项 PROMPTS_DIR 决定（可指向容器挂载的绝对路径）。
本包只负责"怎么读、怎么校验、怎么热更新"。
"""

from functools import lru_cache

from app.prompts.loader import Prompt, PromptConfigError
from app.prompts.registry import PromptKey, PromptRegistry


@lru_cache(maxsize=1)
def get_prompt_registry() -> PromptRegistry:
    """提示词注册表：进程级单例，首次使用时加载并校验。"""
    # 延迟导入，避免循环：app.core.config 在模块级 import 了本包的 PromptKey，
    # 这里若在模块级反向 import，config 会拿到一个还没执行完的包。
    from app.core.config import get_settings

    return PromptRegistry(get_settings().prompts_dir)


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
