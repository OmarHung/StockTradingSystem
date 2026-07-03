"""台股交易成本模型。

- 手續費：券商收取，公定 0.1425%，買賣皆收，單筆最低 20 元。
- 證交稅：政府收取 0.3%，僅賣出收取（當沖為 0.15%，此處以現股波段為主）。
可透過 fee_discount 模擬券商折讓（如 0.28 折 → 有效費率 0.1425% × 0.28）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    fee_rate: float = 0.001425        # 手續費率
    tax_rate: float = 0.003           # 證交稅率（賣出）
    fee_discount: float = 1.0         # 手續費折數（1.0=無折扣，0.28=2.8折）
    min_fee: float = 20.0             # 單筆最低手續費

    def buy_cost(self, amount: float) -> float:
        """買進 amount（元）需額外付出的成本（手續費）。"""
        return max(amount * self.fee_rate * self.fee_discount, self.min_fee)

    def sell_cost(self, amount: float) -> float:
        """賣出 amount（元）需付出的成本（手續費 + 證交稅）。"""
        fee = max(amount * self.fee_rate * self.fee_discount, self.min_fee)
        tax = amount * self.tax_rate
        return fee + tax

    def round_trip_pct(self) -> float:
        """一次完整買賣的成本佔比（粗估，供快速估算最低獲利門檻）。"""
        return self.fee_rate * self.fee_discount * 2 + self.tax_rate
