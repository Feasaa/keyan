"""项目路径基准：把"相对谁"这件事收敛到一处。

为什么不直接在各自的模块里用 `Path(__file__).parents[n]` 拼：
    `__file__` 基准只对"源码树里运行"成立。一旦把内容文件（提示词、模板、
    静态资源）挂进容器、或让运维改到别的目录，拼出来的路径就是错的。
    所以这里只提供一个基准目录，具体位置交给配置项（PROMPTS_DIR 等），
    配置项既可以是相对基准的相对路径，也可以是容器里挂载的绝对路径。
"""

from pathlib import Path

# app/core/paths.py -> core -> app -> 仓库根（也是 uvicorn 的工作目录）
BASE_DIR = Path(__file__).resolve().parents[2]


def resolve_path(value: str | Path) -> Path:
    """相对路径按 BASE_DIR 解析；绝对路径原样返回。

    绝对路径必须原样用：容器里通常把提示词挂到 /app/prompts 并配
    PROMPTS_DIR=/app/prompts，此时再拼 BASE_DIR 会得到一个不存在的路径。
    """
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return BASE_DIR / path
