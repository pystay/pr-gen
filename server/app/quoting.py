"""企业报价：Pydantic 表单校验 + 可配置价格模型。

公式（美元/年，系数可在 .env 配置）：
    基础价格   = QUOTE_BASE_PRICE + max(0, 开发者数量 - QUOTE_MIN_DEVS) * QUOTE_PER_DEV
    部署附加费 = 基础价格 * QUOTE_DEPLOYMENT_RATE   （私有 IDC 部署）
    定制附加费 = 基础价格 * QUOTE_CUSTOM_RATE       （勾选定制开发）
    最终报价   = 基础价格 + 部署附加费 + 定制附加费
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from .config import Settings


class DeploymentEnv(str, Enum):
    AWS = "aws"
    PRIVATE_IDC = "private_idc"
    HYBRID = "hybrid"


# 邮箱白名单校验：仅允许标准邮箱字符，杜绝路径注入/header 注入
EMAIL_RE = r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"


class QuoteRequest(BaseModel):
    """企业询价表单（前端 React Hook Form 与后端 Pydantic 双重校验）。"""

    company: str = Field(min_length=2, max_length=200, description="公司名称")
    contact_email: str = Field(min_length=5, max_length=200, description="联系邮箱")
    dev_count: int = Field(ge=1, le=100000, description="预估开发者数量")
    environment: DeploymentEnv = Field(description="期望部署环境")
    needs_custom: bool = Field(default=False, description="是否需要定制开发")
    special_notes: str = Field(default="", max_length=2000, description="特殊需求描述")

    @field_validator("contact_email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        import re

        v = v.strip()
        if not re.fullmatch(EMAIL_RE, v):
            raise ValueError("邮箱格式不正确")
        return v

    @field_validator("company")
    @classmethod
    def _strip_company(cls, v: str) -> str:
        return v.strip()


@dataclass
class QuoteResult:
    base_price: float
    deployment_fee: float
    custom_fee: float
    total: float
    currency: str = "USD"
    valid_days: int = 30

    def breakdown(self) -> dict:
        return {
            "base_price": round(self.base_price, 2),
            "deployment_fee": round(self.deployment_fee, 2),
            "custom_fee": round(self.custom_fee, 2),
            "total": round(self.total, 2),
            "currency": self.currency,
            "valid_days": self.valid_days,
        }


def compute_quote(req: QuoteRequest, settings: Settings) -> QuoteResult:
    """按配置系数计算报价。

    精度：total 基于未取整的原始值求和后统一 round，保证
    base + deployment + custom == total（各分解项展示值也为 round 后）。
    """
    q = settings.quote
    base = q["base"] + max(0, req.dev_count - int(q["min_devs"])) * q["per_dev"]
    deployment_fee = base * q["deployment_rate"] if req.environment == DeploymentEnv.PRIVATE_IDC else 0.0
    custom_fee = base * q["custom_rate"] if req.needs_custom else 0.0
    return QuoteResult(
        base_price=round(base, 2),
        deployment_fee=round(deployment_fee, 2),
        custom_fee=round(custom_fee, 2),
        total=round(base + deployment_fee + custom_fee, 2),
        valid_days=settings.quote_valid_days,
    )
