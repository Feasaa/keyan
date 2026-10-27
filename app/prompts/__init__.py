"""提示词包：所有提示词与代码分离存放，便于评审与调优。"""

from app.prompts.assistant import ASSISTANT_PROMPT
from app.prompts.registry import PROMPTS, PromptKey, compose_system_prompt, get_prompt
from app.prompts.system import SYSTEM_PROMPT

__all__ = [
    "ASSISTANT_PROMPT",
    "PROMPTS",
    "PromptKey",
    "SYSTEM_PROMPT",
    "compose_system_prompt",
    "get_prompt",
]
