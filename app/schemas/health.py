"""健康检查的数据契约：存活探针与就绪探针分开定义。

两者语义不同，不能合并：
    liveness  进程是否活着 —— 失败应重启容器
    readiness 是否可接收流量 —— 失败只摘流量，不重启
"""

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(..., description="服务状态", examples=["ok"])
    name: str = Field(..., description="服务名", examples=["learn-fastapi"])
    version: str = Field(..., description="版本号", examples=["0.1.0"])


class ReadinessResponse(BaseModel):
    status: str = Field(
        ...,
        description="ready 可全量服务；degraded 可服务但核心能力降级",
        examples=["ready"],
    )
    checks: dict[str, str] = Field(
        ...,
        description="各依赖的检查结果",
        examples=[{"llm": "configured"}],
    )
