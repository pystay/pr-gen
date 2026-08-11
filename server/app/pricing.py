"""定价模块：加载 pricing_config.yaml，计算价格（含促销窗口与锁定老价格）。

设计要点：
- 每次报价/下单时重新加载配置文件 → 改价即时生效，无需重启
- 促销：当前时间在 promotion 窗口内时返回促销价
- 锁定老价格：用户首次订阅时记录 locked_price；后续提价仅影响新用户，
  老用户（price_locked=true）续费仍按 locked_price 计费
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing_config.yaml"

TIERS = ("free", "pro", "team")
CYCLES = ("monthly", "yearly")


class PricingError(RuntimeError):
    pass


@dataclass
class Promotion:
    active: bool
    note: str = ""

    def describe(self) -> str | None:
        return self.note if self.active else None


@dataclass
class PriceQuote:
    tier: str
    cycle: str
    amount: float
    currency: str
    locked: bool = False          # 是否使用了锁定老价格
    locked_price: float | None = None
    promotion_active: bool = False
    promotion_note: str | None = None
    monthly_equivalent: float = 0.0

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "cycle": self.cycle,
            "amount": round(self.amount, 2),
            "currency": self.currency,
            "locked": self.locked,
            "locked_price": self.locked_price,
            "promotion_active": self.promotion_active,
            "promotion_note": self.promotion_note,
            "monthly_equivalent": round(self.monthly_equivalent, 2),
        }


class Pricing:
    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self._data: dict[str, Any] = {}
        self.reload()

    # ---------- 加载 ----------

    def reload(self) -> None:
        if not self.config_path.exists():
            raise PricingError(f"定价配置文件不存在: {self.config_path}")
        try:
            with open(self.config_path, encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise PricingError(f"定价配置文件解析失败: {exc}") from exc
        tiers = self._data.get("tiers") or {}
        missing = [t for t in TIERS if t not in tiers]
        if missing:
            raise PricingError(f"定价配置缺少层级: {missing}")

    @property
    def config_path_str(self) -> str:
        return str(self.config_path)

    # ---------- 查询 ----------

    def tier_features(self, tier: str) -> list[str]:
        t = (self._data.get("tiers") or {}).get(tier) or {}
        return list(t.get("features") or [])

    def free_quota(self) -> int:
        t = (self._data.get("tiers") or {}).get("free") or {}
        return int(t.get("quota_monthly") or 5)

    def _promotion_window(self) -> Promotion:
        promo = self._data.get("promotion") or {}
        if not promo:
            return Promotion(active=False)
        try:
            start = date.fromisoformat(str(promo["start_date"]))
            end = date.fromisoformat(str(promo["end_date"]))
        except (KeyError, ValueError):
            return Promotion(active=False)
        today = date.today()
        active = start <= today <= end
        return Promotion(active=active, note=str(promo.get("note", "")))

    def quote(self, tier: str, cycle: str, currency: str = "CNY",
              locked: bool = False, locked_price: float | None = None,
              locked_currency: str | None = None) -> PriceQuote:
        """计算价格。currency: CNY（国内）/ USD（海外）。

        locked=True 且 locked_price 有值时使用锁定价格（老用户续费）。
        locked_price 语义：**月度价格点**（用户首次付费时的月度等价价格）。
        续费金额 = locked_monthly × 周期月数（monthly=1, yearly=12），
        从而月付与年付切换时价格比例保持一致。
        locked_currency 与请求币种不一致时锁定价不生效（用新价）。
        """
        if tier not in TIERS:
            raise PricingError(f"未知层级: {tier}")
        if cycle not in CYCLES:
            raise PricingError(f"未知计费周期: {cycle}")
        if tier == "free":
            return PriceQuote(tier="free", cycle=cycle, amount=0.0,
                              currency=currency, monthly_equivalent=0.0)

        cycle_months = 12 if cycle == "yearly" else 1

        if currency == "USD":
            table = (self._data.get("international_pricing") or {}).get(tier) or {}
            amount = round(float(table.get(f"price_{cycle}", 0.0)), 2)
            monthly_eq = round(amount / 12, 2) if cycle == "yearly" else amount
            # 锁定价格（海外）：locked_price 为月度点，按周期换算
            if locked and locked_price is not None and locked_currency == "USD":
                amount = round(locked_price * cycle_months, 2)
                return PriceQuote(tier=tier, cycle=cycle, amount=amount,
                                  currency="USD", locked=True, locked_price=locked_price,
                                  monthly_equivalent=round(locked_price, 2))
            return PriceQuote(tier=tier, cycle=cycle, amount=amount,
                              currency="USD", monthly_equivalent=monthly_eq)

        # CNY 国内定价
        table = (self._data.get("tiers") or {}).get(tier) or {}
        amount = round(float(table.get(f"price_{cycle}", 0.0)), 2)

        # 锁定老价格优先（老用户续费；locked_price 为月度价格点）
        if locked and locked_price is not None and locked_currency in (None, "CNY"):
            amount = round(locked_price * cycle_months, 2)
            return PriceQuote(tier=tier, cycle=cycle, amount=amount,
                              currency="CNY", locked=True, locked_price=locked_price,
                              monthly_equivalent=round(locked_price, 2))

        # 促销窗口：仅影响 pro 的月付/年付（按配置的促销价）
        promo = self._promotion_window()
        if promo.active and tier == "pro":
            promo_price = (self._data.get("promotion") or {}).get(f"pro_price_{cycle}")
            if promo_price is not None:
                return PriceQuote(tier=tier, cycle=cycle, amount=float(promo_price),
                                  currency="CNY", promotion_active=True,
                                  promotion_note=promo.note,
                                  monthly_equivalent=(float(promo_price) / 12 if cycle == "yearly" else float(promo_price)))

        monthly_eq = amount / 12 if cycle == "yearly" else amount
        return PriceQuote(tier=tier, cycle=cycle, amount=amount, currency="CNY",
                          monthly_equivalent=monthly_eq)
