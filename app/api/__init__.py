"""接口层（api）：HTTP 协议适配，不写业务规则。

对外只暴露一个 api_router，由 main.py 挂载到 API_PREFIX 之下。
"""

from app.api.router import api_router

__all__ = ["api_router"]
