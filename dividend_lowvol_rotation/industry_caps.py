# -*- coding: utf-8 -*-
"""行业分散与 Beta 暴露约束。"""

from __future__ import annotations

import math

from dividend_lowvol_rotation.config import (
    BETA_BALANCE_ENABLED,
    BETA_LOW_THRESHOLD,
    BETA_MAX_HIGH_FRAC,
    BETA_MIN_LOW_FRAC,
    DEFENSIVE_INDUSTRY_KEYWORDS,
)


def is_defensive_industry(industry: str | None) -> bool:
    if not industry:
        return False
    ind = str(industry).strip()
    return any(k in ind for k in DEFENSIVE_INDUSTRY_KEYWORDS)


def industry_weight_ok(
    industry_counts: dict[str, int],
    industry: str,
    top_n: int,
    max_industry_weight: float,
) -> bool:
    trial = industry_counts.get(industry, 0) + 1
    return trial / top_n <= max_industry_weight


def defensive_weight_ok(
    industry_counts: dict[str, int],
    defensive_count: int,
    industry: str,
    top_n: int,
    max_defensive_weight: float,
) -> bool:
    if not is_defensive_industry(industry):
        return True
    return (defensive_count + 1) / top_n <= max_defensive_weight


def top3_weight_ok(industry_counts: dict[str, int], top_n: int, max_top3_weight: float) -> bool:
    if not industry_counts:
        return True
    sorted_counts = sorted(industry_counts.values(), reverse=True)
    top3 = sum(sorted_counts[:3])
    return top3 / top_n <= max_top3_weight


def is_low_beta(beta: float | None) -> bool:
    if beta is None:
        return False
    return float(beta) <= BETA_LOW_THRESHOLD


def beta_balance_ok(
    low_beta_count: int,
    high_beta_count: int,
    beta: float | None,
    top_n: int,
) -> bool:
    """选股过程中维持低/高 Beta 比例约束。"""
    if not BETA_BALANCE_ENABLED or top_n <= 0 or beta is None:
        return True
    selected = low_beta_count + high_beta_count
    slots_left = top_n - selected
    if is_low_beta(beta):
        return True
    if high_beta_count + 1 > int(top_n * BETA_MAX_HIGH_FRAC) + 1:
        return False
    need_low = math.ceil(top_n * BETA_MIN_LOW_FRAC) - low_beta_count
    if need_low > max(slots_left - 1, 0):
        return False
    return True


def beta_balance_final_ok(low_beta_count: int, high_beta_count: int, top_n: int) -> bool:
    if not BETA_BALANCE_ENABLED or top_n <= 0:
        return True
    if low_beta_count / top_n < BETA_MIN_LOW_FRAC - 1e-9:
        return False
    if high_beta_count / top_n > BETA_MAX_HIGH_FRAC + 1e-9:
        return False
    return True
