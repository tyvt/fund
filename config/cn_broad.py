"""A 股宽基信号阈值。"""

from functools import lru_cache
import os

from config.env import ENV_BOOL_TRUE, _env_float_any, _env_int_any
from config.price_position import (
    BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    BUY_NEAR_YEAR_LOW_RANGE_PCT,
    BUY_NEAR_YEAR_LOW_SPREAD_RELAX,
)

# --- A 股宽基（各指数独立阈值，不回退 CN_BROAD_* 全局默认）---
# 买入：股债利差分位 + 价格位置（多数指标 favorable）；卖出：仅移动止盈。
# 覆盖方式：CN_BROAD_{代码}_*
_CN_BROAD_CFG_SUFFIX = {
    "buy_spread_percentile_min": "BUY_SPREAD_PERCENTILE_MIN",
    "buy_require_spread": "BUY_REQUIRE_SPREAD",
    "buy_min_applicable_criteria": "BUY_MIN_APPLICABLE_CRITERIA",
    "buy_min_pass_score_floor": "BUY_MIN_PASS_SCORE_FLOOR",
    "percentile_window": "PERCENTILE_WINDOW",
    "percentile_min_days": "PERCENTILE_MIN_DAYS",
    "buy_max_above_low_pct": "BUY_MAX_ABOVE_LOW_PCT",
    "buy_low_lookback_days": "BUY_LOW_LOOKBACK_DAYS",
    "buy_high_lookback_days": "BUY_HIGH_LOOKBACK_DAYS",
    "buy_min_drawdown_from_high_pct": "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT",
    "buy_max_year_range_pct": "BUY_MAX_YEAR_RANGE_PCT",
    "buy_near_year_low_range_pct": "BUY_NEAR_YEAR_LOW_RANGE_PCT",
    "buy_near_year_low_spread_relax": "BUY_NEAR_YEAR_LOW_SPREAD_RELAX",
    "buy_near_year_low_above_low_relax": "BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX",
    "buy_near_year_low_drawdown_waive_pct": "BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT",
    "buy_mid_range_position_pct": "BUY_MID_RANGE_POSITION_PCT",
    "buy_mid_range_max_above_low_pct": "BUY_MID_RANGE_MAX_ABOVE_LOW_PCT",
    "buy_range_lookback_days": "BUY_RANGE_LOOKBACK_DAYS",
    "buy_trend_ma_days": "BUY_TREND_MA_DAYS",
    "buy_trend_slope_lookback_days": "BUY_TREND_SLOPE_LOOKBACK_DAYS",
    "buy_trend_min_ma_slope_pct": "BUY_TREND_MIN_MA_SLOPE_PCT",
    "buy_trend_downtrend_max_range_pct": "BUY_TREND_DOWNTREND_MAX_RANGE_PCT",
    "sell_trailing_drawdown_pct": "SELL_TRAILING_DRAWDOWN_PCT",
    "sell_min_unrealized_gain_pct": "SELL_MIN_UNREALIZED_GAIN_PCT",
    "sell_trailing_min_hold_days": "SELL_TRAILING_MIN_HOLD_DAYS",
    "sell_cost_lookback_days": "SELL_COST_LOOKBACK_DAYS",
}

_CN_BROAD_INT_KEYS = {
    "buy_min_applicable_criteria",
    "buy_min_pass_score_floor",
    "percentile_window",
    "percentile_min_days",
    "buy_low_lookback_days",
    "buy_high_lookback_days",
    "buy_range_lookback_days",
    "buy_trend_ma_days",
    "buy_trend_slope_lookback_days",
    "sell_trailing_min_hold_days",
    "sell_cost_lookback_days",
}


def _cn_broad_index_defaults(**overrides):
    """构造单只宽基指数的完整默认阈值（五只指数各自独立，仅用于初始化）。"""
    cfg = {
        "buy_spread_percentile_min": 55,
        "buy_require_spread": True,
        "buy_min_applicable_criteria": 2,
        "buy_min_pass_score_floor": 3,
        "percentile_window": 2520,
        "percentile_min_days": 120,
        "buy_max_above_low_pct": 0.07,
        "buy_low_lookback_days": 120,
        "buy_high_lookback_days": 252,
        "buy_min_drawdown_from_high_pct": 0.14,
        "buy_max_year_range_pct": 0.48,
        "buy_near_year_low_range_pct": 0.20,
        "buy_near_year_low_spread_relax": 10.0,
        "buy_near_year_low_above_low_relax": 0.04,
        "buy_near_year_low_drawdown_waive_pct": 0.12,
        "buy_mid_range_position_pct": 0.45,
        "buy_mid_range_max_above_low_pct": 0.06,
        "buy_range_lookback_days": 252,
        "buy_trend_ma_days": 200,
        "buy_trend_slope_lookback_days": 60,
        "buy_trend_min_ma_slope_pct": -0.02,
        "buy_trend_downtrend_max_range_pct": 0.10,
        "sell_trailing_drawdown_pct": None,
        "sell_min_unrealized_gain_pct": 0.60,
        "sell_trailing_min_hold_days": 90,
        "sell_cost_lookback_days": 252,
    }
    cfg.update(overrides)
    return cfg


_CN_BROAD_PER_INDEX_DEFAULTS = {
    # 中证1000：二次收紧（最严）
    "000852": _cn_broad_index_defaults(
        buy_spread_percentile_min=70,
        buy_max_above_low_pct=0.05,
        buy_min_drawdown_from_high_pct=0.16,
        buy_max_year_range_pct=0.34,
        buy_mid_range_max_above_low_pct=0.04,
        sell_trailing_drawdown_pct=0.10,
        sell_min_unrealized_gain_pct=0.40,
        sell_trailing_min_hold_days=60,
    ),
    # 科创50：夏普偏低但绝对收益高，轻度收紧
    "000688": _cn_broad_index_defaults(
        buy_spread_percentile_min=64,
        buy_max_above_low_pct=0.07,
        buy_min_drawdown_from_high_pct=0.14,
        buy_max_year_range_pct=0.38,
        buy_mid_range_max_above_low_pct=0.05,
        buy_trend_downtrend_max_range_pct=0.06,
        sell_trailing_drawdown_pct=0.125,
        sell_min_unrealized_gain_pct=0.50,
        sell_trailing_min_hold_days=60,
        sell_stages=[
            {"gain_pct": 0.50, "fraction_of_initial": 1 / 3},
            {"gain_pct": 0.80, "fraction_of_initial": 1 / 3},
        ],
    ),
}


@lru_cache(maxsize=None)
def get_cn_broad_signal_config(index_code):
    """读取单只 A 股宽基指数的买入/卖出阈值（仅分指数默认 + 分指数环境变量）。"""
    per_index = _CN_BROAD_PER_INDEX_DEFAULTS.get(index_code)
    if per_index is None:
        raise ValueError(f"未知宽基指数代码: {index_code}")
    cfg = {}
    for key, suffix in _CN_BROAD_CFG_SUFFIX.items():
        default = per_index[key]
        env_names = [f"CN_BROAD_{index_code}_{suffix}"]
        if key in ("buy_require_spread",):
            raw = None
            for name in env_names:
                raw = os.environ.get(name)
                if raw is not None and raw != "":
                    break
            if raw is None or raw == "":
                cfg[key] = default
            else:
                cfg[key] = raw.strip().lower() in ENV_BOOL_TRUE
            continue
        if key in _CN_BROAD_INT_KEYS:
            cfg[key] = _env_int_any(tuple(env_names), default)
            continue
        cfg[key] = _env_float_any(tuple(env_names), default)
    if "sell_stages" in per_index:
        cfg["sell_stages"] = per_index["sell_stages"]
    return cfg


def cn_broad_valuation_sell_enabled(cfg):
    """宽基估值类卖点已移除。"""
    del cfg
    return False

