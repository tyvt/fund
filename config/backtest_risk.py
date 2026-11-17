"""回测风险指标与卖出开关。"""

from config.env import _env_bool, _env_float, _env_int, _env_float_any, _env_int_any

# --- 回测风险指标 ---
BACKTEST_RISK_FREE_RATE = _env_float("BACKTEST_RISK_FREE_RATE", 0.024)
BACKTEST_TRADING_DAYS_PER_YEAR = _env_int("BACKTEST_TRADING_DAYS_PER_YEAR", 252)

# --- 卖出开关（自基日回测有明显超额的指数启用移动止盈）---
CN_BROAD_SELL_ENABLED_CODES = frozenset({"000688"})
US_INDEX_SELL_ENABLED = _env_bool("US_INDEX_SELL_ENABLED", True)
# 分批/移动止盈触发后：持仓浮盈须回落至该阈值及以下才允许再买入（默认关，与无人为限频一致）
SELL_REBUY_GATE_ENABLED = _env_bool("SELL_REBUY_GATE_ENABLED", False)
SELL_REBUY_MAX_GAIN_PCT = _env_float("SELL_REBUY_MAX_GAIN_PCT", 0.30)


def cn_broad_sell_enabled(index_code):
    """单只 A 股宽基是否启用卖出逻辑。"""
    return index_code in CN_BROAD_SELL_ENABLED_CODES


def us_index_sell_enabled(key=None):
    """美股指数是否启用卖出逻辑。"""
    return US_INDEX_SELL_ENABLED


def get_us_index_sell_config(key):
    """美股指数移动止盈参数（key: ndx）。A 方案：仅单一粗移动止盈。"""
    prefix = key.upper()
    return {
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

