"""健康检查的数据契约。"""

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(..., description="服务状态", examples=["ok"])
    name: str = Field(..., description="服务名", examples=["learn-fastapi"])
    version: str = Field(..., description="版本号", examples=["0.1.0"])
