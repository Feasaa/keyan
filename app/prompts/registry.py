"""提示词注册表：按 id 取用，文件改动后自动重载。

两个设计取舍：
    热更新   按文件 mtime 判断是否需要重载，改提示词不必重启服务
    失败兜底 重载失败不覆盖已加载版本 —— 宁可继续用旧提示词，
             也不能因为一次手误让线上进入"无提示词"状态
"""

import logging
from enum import StrEnum
from pathlib import Path

from app.prompts.loader import Prompt, PromptConfigError, load_all

logger = logging.getLogger(__name__)


class PromptKey(StrEnum):
    """已知提示词的 id，用于避免拼错字符串。新增提示词时同步加一行。"""

    CHAT_SYSTEM = "chat_system"


class PromptRegistry:
    """进程内提示词表，启动时全量加载，之后按需重载。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._prompts = load_all(directory)
        self._mtimes = self._snapshot()

    def _snapshot(self) -> dict[Path, float]:
        return {path: path.stat().st_mtime for path in sorted(self._directory.glob("*.md"))}

    def _refresh(self) -> None:
        current = self._snapshot()
        if current == self._mtimes:
            return
        try:
            prompts = load_all(self._directory)
        except PromptConfigError as exc:
            logger.error("提示词重载失败，继续使用旧版本：%s", exc)
            # 记住这次失败的状态：否则每个请求都会重复解析并刷日志，
            # 文件修好后 mtime 再次变化，自然会被重新加载
            self._mtimes = current
            return
        self._prompts = prompts
        self._mtimes = current
        logger.info("提示词已重载：%s", {pid: p.version for pid, p in prompts.items()})

    def get(self, key: PromptKey | str) -> Prompt:
        self._refresh()
        prompt_id = str(key)
        try:
            return self._prompts[prompt_id]
        except KeyError:
            available = ", ".join(sorted(self._prompts))
            raise PromptConfigError(f"未找到提示词 {prompt_id!r}，可用：{available}") from None

    def versions(self) -> dict[str, int]:
        self._refresh()
        return {pid: prompt.version for pid, prompt in self._prompts.items()}
