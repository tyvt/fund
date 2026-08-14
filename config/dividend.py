"""红利指数信号阈值。"""

from functools import lru_cache
import os

from config.env import (
    ENV_BOOL_TRUE,
    _env_bool,
    _env_float,
    _env_float_any,
    _env_int,
    _env_int_any,
    _env_str,
)
from config.price_position import (
    BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    BUY_MID_RANGE_POSITION_PCT,
    BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    BUY_NEAR_YEAR_LOW_RANGE_PCT,
    BUY_NEAR_YEAR_LOW_SPREAD_RELAX,
    BUY_NEAR_YEAR_LOW_PE_RELAX,
)

# --- 红利指数（H30269，按指数拆分阈值）---
# 买入：股息率-国债利差 > 阈值，且利差分位高、PE 分位低、距 N 日低点涨幅不过高（须同时满足）。
# 红利指数仅配置买入阈值，不设卖点。
# 默认阈值经 2016–2025 回测微调：利差 2.8%、H30269 利差分位 40/PE 74，
# 适度放宽价格位置与回撤，买入频次提升且收益不明显降低。
DIVIDEND_BUY_SPREAD_MIN = _env_float_any(
    ("DIVIDEND_BUY_SPREAD_MIN", "BUY_CONDITION_SPREAD"), 0.028
)
DIVIDEND_BUY_SPREAD_PERCENTILE_MIN = _env_float_any(
    ("DIVIDEND_BUY_SPREAD_PERCENTILE_MIN", "BUY_SPREAD_PERCENTILE_MIN"), 38
)
DIVIDEND_BUY_PE_PERCENTILE_MAX = _env_float_any(
    ("DIVIDEND_BUY_PE_PERCENTILE_MAX", "BUY_PE_PERCENTILE_MAX"), 72
)
DIVIDEND_SPREAD_PERCENTILE_WINDOW = _env_int_any(
    ("DIVIDEND_SPREAD_PERCENTILE_WINDOW", "SPREAD_PERCENTILE_WINDOW"), 756
)
DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS = _env_int_any(
    ("DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS", "SPREAD_PERCENTILE_MIN_DAYS"), 60
)
# 近 10 年（约 2520 交易日）利差分位，用于判断当前利差是否偏高
DIVIDEND_SPREAD_10Y_WINDOW = _env_int("DIVIDEND_SPREAD_10Y_WINDOW", 2520)
DIVIDEND_SPREAD_10Y_MIN_DAYS = _env_int("DIVIDEND_SPREAD_10Y_MIN_DAYS", 504)
DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN = _env_float(
    "DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN", 75
)
DIVIDEND_SIGNAL_HISTORY_START = _env_str(
    "DIVIDEND_SIGNAL_HISTORY_START", "20150101"
)
DIVIDEND_BUY_MAX_ABOVE_LOW_PCT = _env_float_any(
    ("DIVIDEND_BUY_MAX_ABOVE_LOW_PCT",), 0.05
)
DIVIDEND_BUY_LOW_LOOKBACK_DAYS = _env_int_any(
    ("DIVIDEND_BUY_LOW_LOOKBACK_DAYS",), 90
)
DIVIDEND_BUY_HIGH_LOOKBACK_DAYS = _env_int_any(
    ("DIVIDEND_BUY_HIGH_LOOKBACK_DAYS",), 252
)
DIVIDEND_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float_any(
    ("DIVIDEND_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT",), 0.12
)
DIVIDEND_BUY_NEAR_YEAR_LOW_RANGE_PCT = _env_float_any(
    ("DIVIDEND_BUY_NEAR_YEAR_LOW_RANGE_PCT",), BUY_NEAR_YEAR_LOW_RANGE_PCT
)
DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_RELAX = _env_float_any(
    ("DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_RELAX",), BUY_NEAR_YEAR_LOW_SPREAD_RELAX
)
DIVIDEND_BUY_NEAR_YEAR_LOW_PE_RELAX = _env_float_any(
    ("DIVIDEND_BUY_NEAR_YEAR_LOW_PE_RELAX",), BUY_NEAR_YEAR_LOW_PE_RELAX
)
# 近1年低位时放宽绝对利差门槛（百分点，如 0.012 = 放宽 1.2%）
DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_MIN_RELAX = _env_float_any(
    ("DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_MIN_RELAX",), 0.012
)
# 处于近1年区间极低位（如 ≤5%）时，可豁免绝对利差硬门槛（股债利率同向时低点利差仍可能偏小）
DIVIDEND_BUY_EXTREME_YEAR_LOW_RANGE_PCT = _env_float_any(
    ("DIVIDEND_BUY_EXTREME_YEAR_LOW_RANGE_PCT",), 0.05
)
# 股息可持续性：拦截「股价暴跌推高股息率」与极端低 PE（盈利恶化嫌疑）
DIVIDEND_BUY_MAX_YIELD_SPIKE = _env_float("DIVIDEND_BUY_MAX_YIELD_SPIKE", 1.50)
DIVIDEND_YIELD_SPIKE_LOOKBACK_DAYS = _env_int("DIVIDEND_YIELD_SPIKE_LOOKBACK_DAYS", 252)
DIVIDEND_BUY_MIN_PE = _env_float("DIVIDEND_BUY_MIN_PE", 5.0)
DIVIDEND_SUSTAINABILITY_ENABLED = _env_bool("DIVIDEND_SUSTAINABILITY_ENABLED", True)
# 近1年区间极低位时豁免股息率飙升检查（保留 PE 下限），避免系统性崩盘黄金坑拒买
DIVIDEND_YIELD_SPIKE_WAIVE_RANGE_PCT = _env_float(
    "DIVIDEND_YIELD_SPIKE_WAIVE_RANGE_PCT", 0.10
)

_DIVIDEND_CFG_SUFFIX = {
    "buy_spread_min": "BUY_SPREAD_MIN",
    "buy_spread_percentile_min": "BUY_SPREAD_PERCENTILE_MIN",
    "buy_pe_percentile_max": "BUY_PE_PERCENTILE_MAX",
    "buy_max_above_low_pct": "BUY_MAX_ABOVE_LOW_PCT",
    "buy_low_lookback_days": "BUY_LOW_LOOKBACK_DAYS",
    "buy_high_lookback_days": "BUY_HIGH_LOOKBACK_DAYS",
    "buy_min_drawdown_from_high_pct": "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT",
    "buy_near_year_low_range_pct": "BUY_NEAR_YEAR_LOW_RANGE_PCT",
    "buy_near_year_low_spread_relax": "BUY_NEAR_YEAR_LOW_SPREAD_RELAX",
    "buy_near_year_low_pe_relax": "BUY_NEAR_YEAR_LOW_PE_RELAX",
    "buy_near_year_low_spread_min_relax": "BUY_NEAR_YEAR_LOW_SPREAD_MIN_RELAX",
    "buy_extreme_year_low_range_pct": "BUY_EXTREME_YEAR_LOW_RANGE_PCT",
    "buy_near_year_low_above_low_relax": "BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX",
    "buy_mid_range_position_pct": "BUY_MID_RANGE_POSITION_PCT",
    "buy_mid_range_max_above_low_pct": "BUY_MID_RANGE_MAX_ABOVE_LOW_PCT",
}

_DIVIDEND_GLOBAL_DEFAULTS = {
    "buy_spread_min": DIVIDEND_BUY_SPREAD_MIN,
    "buy_spread_percentile_min": DIVIDEND_BUY_SPREAD_PERCENTILE_MIN,
    "buy_pe_percentile_max": DIVIDEND_BUY_PE_PERCENTILE_MAX,
    "buy_max_above_low_pct": DIVIDEND_BUY_MAX_ABOVE_LOW_PCT,
    "buy_low_lookback_days": DIVIDEND_BUY_LOW_LOOKBACK_DAYS,
    "buy_high_lookback_days": DIVIDEND_BUY_HIGH_LOOKBACK_DAYS,
    "buy_min_drawdown_from_high_pct": DIVIDEND_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
    "buy_near_year_low_range_pct": DIVIDEND_BUY_NEAR_YEAR_LOW_RANGE_PCT,
    "buy_near_year_low_spread_relax": DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_RELAX,
    "buy_near_year_low_pe_relax": DIVIDEND_BUY_NEAR_YEAR_LOW_PE_RELAX,
    "buy_near_year_low_spread_min_relax": DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_MIN_RELAX,
    "buy_extreme_year_low_range_pct": DIVIDEND_BUY_EXTREME_YEAR_LOW_RANGE_PCT,
    "buy_near_year_low_above_low_relax": BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    "buy_mid_range_position_pct": BUY_MID_RANGE_POSITION_PCT,
    "buy_mid_range_max_above_low_pct": BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
}

_DIVIDEND_PER_INDEX_DEFAULTS = {
    "H30269": {
        "buy_spread_percentile_min": 40,
        "buy_pe_percentile_max": 74,
        "buy_max_above_low_pct": 0.06,
        "buy_min_drawdown_from_high_pct": 0.10,
    },
}


def get_dividend_signal_config(index_code):
    """读取单只红利指数的买入阈值（分指数默认 + 环境变量覆盖）。"""
    per_index = _DIVIDEND_PER_INDEX_DEFAULTS.get(index_code, {})
    cfg = {}
    int_keys = {"buy_low_lookback_days", "buy_high_lookback_days"}
    for key, suffix in _DIVIDEND_CFG_SUFFIX.items():
        default = per_index.get(key, _DIVIDEND_GLOBAL_DEFAULTS[key])
        env_names = [f"DIVIDEND_{index_code}_{suffix}", f"DIVIDEND_{suffix}"]
        if key == "buy_spread_min":
            env_names.append("BUY_CONDITION_SPREAD")
        elif key == "buy_spread_percentile_min":
            env_names.append("BUY_SPREAD_PERCENTILE_MIN")
        elif key == "buy_pe_percentile_max":
            env_names.append("BUY_PE_PERCENTILE_MAX")
        if key in int_keys:
            cfg[key] = _env_int_any(tuple(env_names), default)
        else:
            cfg[key] = _env_float_any(tuple(env_names), default)
    return cfg


# 兼容旧变量名
BUY_CONDITION_SPREAD = DIVIDEND_BUY_SPREAD_MIN
BUY_SPREAD_PERCENTILE_MIN = DIVIDEND_BUY_SPREAD_PERCENTILE_MIN
BUY_PE_PERCENTILE_MAX = DIVIDEND_BUY_PE_PERCENTILE_MAX
SPREAD_PERCENTILE_WINDOW = DIVIDEND_SPREAD_PERCENTILE_WINDOW
SPREAD_PERCENTILE_MIN_DAYS = DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS
