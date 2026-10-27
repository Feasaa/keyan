"""提示词加载器：正文与元数据放文件里，启动时校验，不合法直接失败。

文件格式（Markdown + front matter）：

    ---
    id: chat_system
    version: 1
    description: 对话场景的系统提示词
    ---
    正文……

只解析 `key: value` 形式的简单元数据，不引入 YAML 依赖 ——
元数据只有三个固定字段，为它装一个解析库不值得。

校验规则（任一不满足都抛 PromptConfigError，让问题在启动时暴露）：
    id 必须存在且与文件名一致   防止改文件名后引用错位
    version 必须是正整数        版本是排障的锚点，不能是自由文本
    正文不能为空                空提示词会让模型行为完全失控
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "files"

_DELIMITER = "---"


class PromptConfigError(RuntimeError):
    """提示词配置非法：缺字段、id 与文件名不一致、正文为空、id 重复等。"""


@dataclass(frozen=True)
class Prompt:
    id: str
    version: int
    description: str
    text: str
    source: Path

    @property
    def checksum(self) -> str:
        """正文摘要，用于日志比对"这次跑的到底是哪份内容"。"""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]


def _split_front_matter(raw: str, source: Path) -> tuple[dict[str, str], str]:
    if not raw.lstrip().startswith(_DELIMITER):
        raise PromptConfigError(f"{source.name} 缺少 front matter（首行应为 ---）")

    _, _, rest = raw.lstrip().partition("\n")
    meta_block, separator, body = rest.partition(f"\n{_DELIMITER}")
    if not separator:
        raise PromptConfigError(f"{source.name} 的 front matter 未闭合（缺少结束的 ---）")

    meta: dict[str, str] = {}
    for line in meta_block.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise PromptConfigError(f"{source.name} 的元数据行格式错误：{line!r}")
        meta[key.strip()] = value.strip()

    return meta, body.lstrip("\n")


def load_prompt(path: Path) -> Prompt:
    """读取并校验单个提示词文件。"""
    meta, body = _split_front_matter(path.read_text(encoding="utf-8"), path)

    prompt_id = meta.get("id", "")
    if not prompt_id:
        raise PromptConfigError(f"{path.name} 缺少 id")
    if prompt_id != path.stem:
        raise PromptConfigError(
            f"{path.name} 的 id 必须与文件名一致（当前 id={prompt_id!r}）"
        )

    raw_version = meta.get("version", "")
    if not raw_version.isdigit() or int(raw_version) < 1:
        raise PromptConfigError(
            f"{path.name} 的 version 必须是正整数（当前 {raw_version!r}）"
        )

    text = body.strip()
    if not text:
        raise PromptConfigError(f"{path.name} 正文为空")

    return Prompt(
        id=prompt_id,
        version=int(raw_version),
        description=meta.get("description", ""),
        text=text,
        source=path,
    )


def load_all(directory: Path = PROMPTS_DIR) -> dict[str, Prompt]:
    """加载目录下全部提示词，按 id 建索引。"""
    if not directory.is_dir():
        raise PromptConfigError(f"提示词目录不存在：{directory}")

    prompts: dict[str, Prompt] = {}
    for path in sorted(directory.glob("*.md")):
        prompt = load_prompt(path)
        if prompt.id in prompts:
            raise PromptConfigError(f"提示词 id 重复：{prompt.id}")
        prompts[prompt.id] = prompt

    if not prompts:
        raise PromptConfigError(f"提示词目录为空：{directory}")

    return prompts
