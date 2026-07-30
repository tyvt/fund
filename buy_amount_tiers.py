"""买入金额分档：按当日价格位置/估值在基准金额上乘以系数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

# 默认四档：年区间位置越低（越接近年内低点）投入越多
DEFAULT_TIER_SCHEME = "range_4"

# scheme_name -> [(max_position, multiplier), ...] 按 position 升序，取第一个满足 position <= max
TIER_SCHEMES: dict[str, list[tuple[float, float]]] = {
    # 保守：仅在极低位明显加仓
    "range_3_conservative": [
        (0.20, 1.40),
        (0.40, 1.00),
        (1.00, 0.75),
    ],
    # 默认四档
    "range_4": [
        (0.18, 1.50),
        (0.32, 1.25),
        (0.48, 1.00),
        (1.00, 0.70),
    ],
    # 激进：低位大幅加仓
    "range_4_aggressive": [
        (0.15, 1.80),
        (0.28, 1.40),
        (0.42, 1.00),
        (1.00, 0.60),
    ],
    # 温和：波动较小，总投入更接近固定金额
    "range_4_mild": [
        (0.22, 1.30),
        (0.38, 1.10),
        (0.52, 1.00),
        (1.00, 0.85),
    ],
    # 六档细分：高位少买、低位多买，梯度更平滑
    "range_6_fine": [
        (0.15, 1.40),
        (0.25, 1.25),
        (0.35, 1.12),
        (0.45, 1.00),
        (0.55, 0.92),
        (1.00, 0.80),
    ],
    # 八档细分：限购场景下进一步压低高位单次金额
    "range_8_fine": [
        (0.12, 1.45),
        (0.20, 1.30),
        (0.28, 1.18),
        (0.36, 1.08),
        (0.44, 1.00),
        (0.52, 0.92),
        (0.60, 0.85),
        (1.00, 0.75),
    ],
}


@dataclass(frozen=True)
class TierScheme:
    name: str
    tiers: tuple[tuple[float, float], ...]

    def multiplier(self, position: float) -> float:
        for max_pos, mult in self.tiers:
            if position <= max_pos:
                return mult
        return self.tiers[-1][1]


def _safe_float(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def row_price_position(row) -> float | None:
    """买入日价格位置：0=近年内低点，1=近年内高点。"""
    yr = _safe_float(row.get("year_range_position") if hasattr(row, "get") else None)
    if yr is not None:
        return max(0.0, min(1.0, yr))
    pal = _safe_float(row.get("pct_above_low") if hasattr(row, "get") else None)
    if pal is not None:
        # 距低点涨幅 0~15% 映射到 0~1
        return max(0.0, min(1.0, pal / 0.15))
    return None


def row_cheapness_score(row) -> float:
    """综合便宜度：越低越便宜；无数据时取 0.5（标准档）。"""
    pos = row_price_position(row)
    if pos is not None:
        return pos
    pe = _safe_float(row.get("pe_percentile") if hasattr(row, "get") else None)
    if pe is not None:
        return max(0.0, min(1.0, pe / 100.0))
    return 0.5


def get_tier_scheme(name: str | None = None) -> TierScheme:
    key = name or DEFAULT_TIER_SCHEME
    tiers = TIER_SCHEMES.get(key)
    if tiers is None:
        raise ValueError(f"未知分档方案: {key}，可选: {', '.join(TIER_SCHEMES)}")
    return TierScheme(name=key, tiers=tuple(tiers))


def tier_multiplier(row, scheme: str | TierScheme | None = None) -> float:
    s = scheme if isinstance(scheme, TierScheme) else get_tier_scheme(scheme)
    return s.multiplier(row_cheapness_score(row))


def resolve_tiered_amount(
    base_amount: float,
    row,
    scheme: str | TierScheme | None = None,
    *,
    min_amount: float = 10.0,
    max_amount: float | None = None,
) -> float:
    """基准金额 × 分档系数，并限制单笔上下限。"""
    if base_amount <= 0:
        return 0.0
    amt = base_amount * tier_multiplier(row, scheme)
    if max_amount is not None:
        amt = min(amt, max_amount)
    return max(min_amount, round(amt))


def make_amount_fn(
    base_amount: float,
    scheme: str | TierScheme | None = None,
    *,
    scale: float = 1.0,
) -> Callable:
    """生成 simulate_trades 可用的 amount_fn(row)。"""
    effective_base = base_amount * scale

    def _fn(row):
        return resolve_tiered_amount(effective_base, row, scheme)

    return _fn


def estimate_avg_multiplier(
    panel,
    start_date,
    end_date,
    buy_fn,
    scheme: str | TierScheme | None = None,
    date_col: str = "date",
) -> float:
    """历史买入日平均分档系数，用于将基准金额缩放至与固定投入总额接近。"""
    from backtest_trade_signals import _filter_panel

    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty or buy_fn is None:
        return 1.0
    mults = []
    for _, row in sample.iterrows():
        if buy_fn(row):
            mults.append(tier_multiplier(row, scheme))
    if not mults:
        return 1.0
    return sum(mults) / len(mults)


def scale_base_for_budget(
    base_amount: float,
    avg_multiplier: float,
    target_total: float,
    buy_count: int,
) -> float:
    """按目标总投入与历史平均分档系数，反推缩放后的基准金额。"""
    if buy_count <= 0 or avg_multiplier <= 0:
        return base_amount
    implied = buy_count * base_amount * avg_multiplier
    if implied <= 0:
        return base_amount
    factor = target_total / implied
    return base_amount * factor


def format_tier_formula(scheme: str | TierScheme | None = None) -> str:
    """单行分档查表说明，供实盘按当日年区间位置自行计算。"""
    s = scheme if isinstance(scheme, TierScheme) else get_tier_scheme(scheme)
    parts = [f"≤{max_pos:.0%}→{mult:.2f}×" for max_pos, mult in s.tiers]
    return f"{s.name}：" + "，".join(parts)


def format_tier_table(scheme: str | TierScheme | None = None) -> str:
    s = scheme if isinstance(scheme, TierScheme) else get_tier_scheme(scheme)
    lines = [
        f"分档方案 **{s.name}**（按年区间位置 / 距低点涨幅）：",
        "",
        "| 价格位置 | 含义 | 投入系数 |",
        "| ---: | --- | ---: |",
    ]
    for i, (max_pos, mult) in enumerate(s.tiers):
        prev = 0.0 if i == 0 else s.tiers[i - 1][0]
        if i == 0:
            desc = "近年内低位"
        elif i == len(s.tiers) - 1:
            desc = "偏高/放宽买入"
        else:
            desc = "偏低" if mult >= 1.0 else "标准"
        lines.append(f"| {prev:.0%}–{max_pos:.0%} | {desc} | **{mult:.2f}×** |")
    return "\n".join(lines)
