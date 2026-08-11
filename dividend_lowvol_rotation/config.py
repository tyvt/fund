"""红利低波轮动策略参数（参考 EasyXT 五层筛选，无交易接入）。"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_DIR / "cache" / "dividend_lowvol"
BACKTEST_OUTPUT_DIR = PROJECT_DIR / "output" / "dividend_lowvol"

ENV_BOOL_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ENV_BOOL_TRUE


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip()


# --- 第二层：股息率 ---
MIN_DIVIDEND_YIELD_PCT = _env_float("DLV_MIN_DIVIDEND_YIELD_PCT", 2.0)
# latest=最近一次派息；ttm=近12个月累计；auto=最新派息超过空窗期则切 ttm
DIVIDEND_YIELD_MODE = _env_str("DLV_DIVIDEND_YIELD_MODE", "auto").lower()
TTM_LOOKBACK_DAYS = _env_int("DLV_TTM_LOOKBACK_DAYS", 365)
LATEST_DIVIDEND_STALE_DAYS = _env_int("DLV_LATEST_DIVIDEND_STALE_DAYS", 365)

EX_DATE_COOLDOWN_ENABLED = _env_bool("DLV_EX_DATE_COOLDOWN_ENABLED", True)
EX_DATE_COOLDOWN_DAYS = _env_int("DLV_EX_DATE_COOLDOWN_DAYS", 5)

# --- 动态阈值 ---
DYNAMIC_THRESHOLD_ENABLED = _env_bool("DLV_DYNAMIC_THRESHOLD_ENABLED", True)
MIN_DIVIDEND_YIELD_FLOOR_PCT = _env_float("DLV_MIN_DIVIDEND_YIELD_FLOOR_PCT", 2.0)
MIN_YIELD_SPREAD_OVER_BOND_PCT = _env_float("DLV_MIN_YIELD_SPREAD_OVER_BOND_PCT", 0.5)
DYNAMIC_VOL_ENABLED = _env_bool("DLV_DYNAMIC_VOL_ENABLED", True)
MARKET_VOL_MEDIAN_MULT = _env_float("DLV_MARKET_VOL_MEDIAN_MULT", 1.5)
MAX_VOL_CEILING_PCT = _env_float("DLV_MAX_VOL_CEILING_PCT", 50.0)
MIN_VOL_FLOOR_PCT = _env_float("DLV_MIN_VOL_FLOOR_PCT", 15.0)

# --- 第三层：低波（静态兜底）---
VOL_LOOKBACK_DAYS = _env_int("DLV_VOL_LOOKBACK_DAYS", 60)
VOL_TRADING_DAYS_PER_YEAR = _env_int("DLV_VOL_TRADING_DAYS_PER_YEAR", 220)
MAX_ANNUALIZED_VOL_PCT = _env_float("DLV_MAX_ANNUALIZED_VOL_PCT", 50.0)
PRICE_HISTORY_BUFFER_DAYS = _env_int("DLV_PRICE_HISTORY_BUFFER_DAYS", 120)

# --- 基本面 ---
FUNDAMENTAL_FILTER_ENABLED = _env_bool("DLV_FUNDAMENTAL_FILTER_ENABLED", True)
MIN_ROE_PCT = _env_float("DLV_MIN_ROE_PCT", 8.0)
MIN_PROFIT_YOY_PCT = _env_float("DLV_MIN_PROFIT_YOY_PCT", -10.0)
OCF_QUALITY_FILTER_ENABLED = _env_bool("DLV_OCF_QUALITY_FILTER_ENABLED", False)
MIN_OCF_TO_PROFIT = _env_float("DLV_MIN_OCF_TO_PROFIT", 1.0)

# --- 第四层：排名打分 ---
YIELD_RANK_WEIGHT = _env_float("DLV_YIELD_RANK_WEIGHT", 1.0)
VOL_RANK_WEIGHT = _env_float("DLV_VOL_RANK_WEIGHT", 0.5)
DYNAMIC_WEIGHT_ENABLED = _env_bool("DLV_DYNAMIC_WEIGHT_ENABLED", True)
YIELD_WEIGHT_BASE = _env_float("DLV_YIELD_WEIGHT_BASE", 1.0)
VOL_WEIGHT_BASE = _env_float("DLV_VOL_WEIGHT_BASE", 0.5)
BOND_YIELD_REF_PCT = _env_float("DLV_BOND_YIELD_REF_PCT", 2.5)
MARKET_VOL_REF_PCT = _env_float("DLV_MARKET_VOL_REF_PCT", 25.0)
YIELD_WEIGHT_BOND_SENS = _env_float("DLV_YIELD_WEIGHT_BOND_SENS", 0.15)
VOL_WEIGHT_MARKET_SENS = _env_float("DLV_VOL_WEIGHT_MARKET_SENS", 0.01)

# --- 第五层：调仓 ---
TOP_N_BUY = _env_int("DLV_TOP_N_BUY", 20)
SELL_RANK_MULTIPLIER = _env_int("DLV_SELL_RANK_MULTIPLIER", 2)
SELL_RANK_BUFFER = _env_int("DLV_SELL_RANK_BUFFER", TOP_N_BUY * SELL_RANK_MULTIPLIER)

# --- 行业分散 ---
INDUSTRY_CAP_ENABLED = _env_bool("DLV_INDUSTRY_CAP_ENABLED", True)
MAX_INDUSTRY_WEIGHT = _env_float("DLV_MAX_INDUSTRY_WEIGHT", 0.34)
# sw=申万一级；csrc=证监会；sw_fallback=申万优先、失败降级证监会
INDUSTRY_SOURCE = _env_str("DLV_INDUSTRY_SOURCE", "sw_fallback").lower()
INDUSTRY_CACHE_MAX_AGE_DAYS = _env_int("DLV_INDUSTRY_CACHE_MAX_AGE_DAYS", 7)

# --- 买入价区间 ---
BUY_RANGE_ABOVE_LOW_PCT = _env_float("DLV_BUY_RANGE_ABOVE_LOW_PCT", 0.03)
BUY_RANGE_BELOW_CURRENT_PCT = _env_float("DLV_BUY_RANGE_BELOW_CURRENT_PCT", 0.01)

# --- 交易成本 ---
COMMISSION_RATE = _env_float("DLV_COMMISSION_RATE", 0.0000854)
MIN_COMMISSION_CNY = _env_float("DLV_MIN_COMMISSION_CNY", 5.0)
LOT_SIZE = _env_int("DLV_LOT_SIZE", 100)  # A 股最小交易单位（手 = 100 股）
DIVIDEND_TAX_ENABLED = _env_bool("DLV_DIVIDEND_TAX_ENABLED", True)
DIVIDEND_TAX_MONTH_DAYS = _env_int("DLV_DIVIDEND_TAX_MONTH_DAYS", 30)
DIVIDEND_TAX_YEAR_DAYS = _env_int("DLV_DIVIDEND_TAX_YEAR_DAYS", 365)
PORTFOLIO_CAPITAL_CNY = _env_float("DLV_PORTFOLIO_CAPITAL_CNY", 0.0)
ESTIMATE_TURNOVER_FRACTION = _env_float("DLV_ESTIMATE_TURNOVER_FRACTION", 0.5)

# --- 回测 ---
BACKTEST_START = _env_str("DLV_BACKTEST_START", "2018-01-01")
BACKTEST_REBALANCE_DAYS = _env_int("DLV_BACKTEST_REBALANCE_DAYS", 20)
BACKTEST_INITIAL_CAPITAL = _env_float("DLV_BACKTEST_INITIAL_CAPITAL", 100_000.0)
BACKTEST_YEARS = _env_int("DLV_BACKTEST_YEARS", 5)
BACKTEST_PREFETCH_SIZE = _env_int("DLV_BACKTEST_PREFETCH_SIZE", 150)

FHPS_REPORT_DATES = tuple(
    d.strip()
    for d in os.environ.get(
        "DLV_FHPS_REPORT_DATES",
        "20251231,20250630,20241231,20231231,20221231,20211231,20201231,"
        "20191231,20181231,20171231,20161231,20151231,20141231,20131231",
    ).split(",")
    if d.strip()
)


def resolve_fhps_report_dates(backtest_start: str | None = None) -> tuple[str, ...]:
    """合并配置的报告期与回测起点所需的额外批次（年报除权通常滞后约一年）。"""
    dates = set(FHPS_REPORT_DATES)
    if backtest_start:
        start_year = int(str(backtest_start)[:4])
        for year in range(start_year - 2, start_year + 1):
            dates.add(f"{year}1231")
            dates.add(f"{year}0630")
    return tuple(sorted(dates, reverse=True))

BAOSTOCK_WORKERS = _env_int("DLV_BAOSTOCK_WORKERS", 1)
BAOSTOCK_BATCH_SLEEP_SEC = _env_float("DLV_BAOSTOCK_BATCH_SLEEP_SEC", 0.02)
TENCENT_QUOTE_BATCH = _env_int("DLV_TENCENT_QUOTE_BATCH", 80)
FINANCIAL_FETCH_SLEEP_SEC = _env_float("DLV_FINANCIAL_FETCH_SLEEP_SEC", 0.05)
SW_INDUSTRY_FETCH_SLEEP_SEC = _env_float("DLV_SW_INDUSTRY_FETCH_SLEEP_SEC", 0.08)


def resolve_sell_rank(top_n: int, sell_rank: int | None = None) -> int:
    if sell_rank is not None and sell_rank > 0:
        return sell_rank
    return max(top_n * SELL_RANK_MULTIPLIER, top_n + 1)
