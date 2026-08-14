"""回测风险指标与卖出开关。"""

import os
from config.env import _env_bool, _env_float, _env_int, _env_float_any, _env_int_any, _env_str

# --- 回测风险指标 ---
BACKTEST_RISK_FREE_RATE = _env_float("BACKTEST_RISK_FREE_RATE", 0.024)
BACKTEST_TRADING_DAYS_PER_YEAR = _env_int("BACKTEST_TRADING_DAYS_PER_YEAR", 252)

# --- 卖出开关（自基日回测有明显超额的指数启用移动止盈）---
CN_BROAD_SELL_ENABLED_CODES = frozenset({"000852", "000688"})
DIVIDEND_SELL_ENABLED = _env_bool("DIVIDEND_SELL_ENABLED", True)
US_INDEX_SELL_ENABLED = _env_bool("US_INDEX_SELL_ENABLED", True)
# 分批/移动止盈触发后：持仓浮盈须回落至该阈值及以下才允许再买入（默认关，与无人为限频一致）
SELL_REBUY_GATE_ENABLED = _env_bool("SELL_REBUY_GATE_ENABLED", False)
SELL_REBUY_MAX_GAIN_PCT = _env_float("SELL_REBUY_MAX_GAIN_PCT", 0.30)

# --- 轮动卖出：仅在有其他指数买点时卖出，并优先复用释放资金 ---
ROTATION_SELL_ENABLED = _env_bool("ROTATION_SELL_ENABLED", True)
ROTATION_MARGINAL_HURDLE_ANN_PCT = _env_float(
    "ROTATION_MARGINAL_HURDLE_ANN_PCT", 10.0
)

# --- 牛熊市场状态（基于年区间位置 + MA 斜率，无额外数据源）---
MARKET_REGIME_ENABLED = _env_bool("MARKET_REGIME_ENABLED", False)
MARKET_REGIME_PROXY_CODES = tuple(
    c.strip()
    for c in _env_str("MARKET_REGIME_PROXY_CODES", "000852,399006,NDX").split(",")
    if c.strip()
)
MARKET_REGIME_BULL_RANGE_MIN = _env_float("MARKET_REGIME_BULL_RANGE_MIN", 0.58)
MARKET_REGIME_BULL_MA_SLOPE_MIN = _env_float("MARKET_REGIME_BULL_MA_SLOPE_MIN", 0.0)
MARKET_REGIME_BEAR_RANGE_MAX = _env_float("MARKET_REGIME_BEAR_RANGE_MAX", 0.40)
MARKET_REGIME_BEAR_MA_SLOPE_MAX = _env_float("MARKET_REGIME_BEAR_MA_SLOPE_MAX", -0.025)
# 牛市：少买、提高轮动估值门槛（难卖）；熊市：多买、降低门槛（易轮动）
MARKET_REGIME_BULL_BUY_MULT = _env_float("MARKET_REGIME_BULL_BUY_MULT", 0.90)
MARKET_REGIME_BEAR_BUY_MULT = _env_float("MARKET_REGIME_BEAR_BUY_MULT", 1.12)
MARKET_REGIME_BULL_ROTATION_HURDLE = _env_float(
    "MARKET_REGIME_BULL_ROTATION_HURDLE", 14.0
)
MARKET_REGIME_BEAR_ROTATION_HURDLE = _env_float(
    "MARKET_REGIME_BEAR_ROTATION_HURDLE", 7.0
)


def cn_broad_sell_enabled(index_code):
    """单只 A 股宽基是否启用卖出逻辑。"""
    return index_code in CN_BROAD_SELL_ENABLED_CODES


def dividend_sell_enabled(index_code=None):
    """红利指数是否启用卖出逻辑。"""
    return DIVIDEND_SELL_ENABLED


def us_index_sell_enabled(key=None):
    """美股指数是否启用卖出逻辑。"""
    return US_INDEX_SELL_ENABLED


# --- 红利指数卖出（仅移动止盈）---
DIVIDEND_SELL_TRAILING_DRAWDOWN_PCT = _env_float(
    "DIVIDEND_SELL_TRAILING_DRAWDOWN_PCT", 0.10
)
DIVIDEND_SELL_MIN_UNREALIZED_GAIN_PCT = _env_float(
    "DIVIDEND_SELL_MIN_UNREALIZED_GAIN_PCT", 0.40
)
DIVIDEND_SELL_TRAILING_MIN_HOLD_DAYS = _env_int(
    "DIVIDEND_SELL_TRAILING_MIN_HOLD_DAYS", 60
)


def get_dividend_sell_config(index_code):
    """红利指数移动止盈参数。"""
    return {
        "sell_trailing_drawdown_pct": _env_float_any(
            (
                f"DIVIDEND_{index_code}_SELL_TRAILING_DRAWDOWN_PCT",
                "DIVIDEND_SELL_TRAILING_DRAWDOWN_PCT",
            ),
            DIVIDEND_SELL_TRAILING_DRAWDOWN_PCT,
        ),
        "sell_min_unrealized_gain_pct": _env_float_any(
            (
                f"DIVIDEND_{index_code}_SELL_MIN_UNREALIZED_GAIN_PCT",
                "DIVIDEND_SELL_MIN_UNREALIZED_GAIN_PCT",
            ),
            DIVIDEND_SELL_MIN_UNREALIZED_GAIN_PCT,
        ),
        "sell_trailing_min_hold_days": _env_int_any(
            (
                f"DIVIDEND_{index_code}_SELL_TRAILING_MIN_HOLD_DAYS",
                "DIVIDEND_SELL_TRAILING_MIN_HOLD_DAYS",
            ),
            DIVIDEND_SELL_TRAILING_MIN_HOLD_DAYS,
        ),
    }


def get_us_index_sell_config(key):
    """美股指数移动止盈与估值卖点参数（key: ndx / spx）。"""
    prefix = key.upper()
    return {
        "sell_trailing_pe_percentile_min": _env_float(
            f"{prefix}_SELL_TRAILING_PE_PERCENTILE_MIN", 88
        ),
        "sell_max_above_low_pct": _env_float(f"{prefix}_SELL_MAX_ABOVE_LOW_PCT", 0.30),
        "sell_trailing_drawdown_pct": _env_float(
            f"{prefix}_SELL_TRAILING_DRAWDOWN_PCT", 0.12
        ),
        "sell_min_unrealized_gain_pct": _env_float(
            f"{prefix}_SELL_MIN_UNREALIZED_GAIN_PCT", 0.50
        ),
        "sell_trailing_min_hold_days": _env_int(
            f"{prefix}_SELL_TRAILING_MIN_HOLD_DAYS", 90
        ),
    }

