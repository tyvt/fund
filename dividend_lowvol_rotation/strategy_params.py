# -*- coding: utf-8 -*-
"""策略可调参数（回测 / 优化用）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from dividend_lowvol_rotation.config import (
    BACKTEST_REBALANCE_DAYS,
    SELL_RANK_MULTIPLIER,
    TOP_N_BUY,
    resolve_sell_rank,
)


@dataclass
class StrategyParams:
    """覆盖 config 默认值的策略参数；None 表示使用 config 默认。"""

    top_n: int | None = None
    sell_rank: int | None = None
    sell_rank_multiplier: float | None = None
    rebalance_days: int | None = None
    min_dividend_yield_pct: float | None = None
    max_annualized_vol_pct: float | None = None
    yield_rank_weight: float | None = None
    vol_rank_weight: float | None = None
    min_roe_pct: float | None = None
    min_profit_yoy_pct: float | None = None
    max_industry_weight: float | None = None
    market_vol_median_mult: float | None = None
    min_yield_spread_over_bond_pct: float | None = None
    dynamic_vol_enabled: bool | None = None
    dynamic_weight_enabled: bool | None = None
    industry_cap_enabled: bool | None = None
    stop_atr_multiplier: float | None = None

    def resolved_top_n(self, default: int = TOP_N_BUY) -> int:
        return int(self.top_n if self.top_n is not None else default)

    def resolved_rebalance_days(self, default: int = BACKTEST_REBALANCE_DAYS) -> int:
        return int(self.rebalance_days if self.rebalance_days is not None else default)

    def resolved_sell_rank(self, top_n: int | None = None) -> int:
        if self.sell_rank is not None and self.sell_rank > 0:
            return int(self.sell_rank)
        tn = top_n if top_n is not None else self.resolved_top_n()
        mult = self.sell_rank_multiplier if self.sell_rank_multiplier is not None else SELL_RANK_MULTIPLIER
        return resolve_sell_rank(tn, max(int(round(tn * mult)), tn + 1))

    def merge(self, overrides: dict[str, Any] | None) -> StrategyParams:
        if not overrides:
            return self
        data = asdict(self)
        for k, v in overrides.items():
            if k in data and v is not None:
                data[k] = v
        return StrategyParams(**data)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def summary(self) -> str:
        parts = []
        for f in fields(self):
            val = getattr(self, f.name)
            if val is not None:
                parts.append(f"{f.name}={val}")
        return ", ".join(parts) if parts else "default"


def defaults() -> StrategyParams:
    return StrategyParams()
