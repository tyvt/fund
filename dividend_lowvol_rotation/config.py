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
MIN_DIVIDEND_YIELD_PCT = _env_float("DLV_MIN_DIVIDEND_YIELD_PCT", 3.09)
MIN_DIVIDEND_YIELD_FLOOR_PCT = _env_float("DLV_MIN_DIVIDEND_YIELD_FLOOR_PCT", 3.09)
# latest=最近一次派息；ttm=近12个月累计；auto=最新派息超过空窗期则切 ttm
DIVIDEND_YIELD_MODE = _env_str("DLV_DIVIDEND_YIELD_MODE", "auto").lower()
TTM_LOOKBACK_DAYS = _env_int("DLV_TTM_LOOKBACK_DAYS", 365)
LATEST_DIVIDEND_STALE_DAYS = _env_int("DLV_LATEST_DIVIDEND_STALE_DAYS", 365)

DYNAMIC_VOL_ENABLED = _env_bool("DLV_DYNAMIC_VOL_ENABLED", True)
MARKET_VOL_MEDIAN_MULT = _env_float("DLV_MARKET_VOL_MEDIAN_MULT", 1.68)
MAX_VOL_CEILING_PCT = _env_float("DLV_MAX_VOL_CEILING_PCT", 40.0)
MIN_VOL_FLOOR_PCT = _env_float("DLV_MIN_VOL_FLOOR_PCT", 15.0)

# 市值分层 + 中小盘仓位上限（消融保留：关闭后年化 -0.53pp）
MV_TIER_CAP_ENABLED = _env_bool("DLV_MV_TIER_CAP_ENABLED", True)
MV_TIER_LARGE_CNY = _env_float("DLV_MV_TIER_LARGE_CNY", 20_000_000_000.0)  # 200 亿
MV_TIER_SMALL_MAX_WEIGHT = _env_float("DLV_MV_TIER_SMALL_MAX_WEIGHT", 0.40)


def panel_factor_cache_key() -> str:
    """Panel 缓存键（参数扫描时需区分市值/行业/Beta 配置）。"""
    return "|".join(
        str(v)
        for v in (
            MV_TIER_CAP_ENABLED,
            MV_TIER_LARGE_CNY,
            MV_TIER_SMALL_MAX_WEIGHT,
            MAX_INDUSTRY_WEIGHT,
            MAX_DEFENSIVE_INDUSTRY_WEIGHT,
            MAX_TOP3_INDUSTRY_WEIGHT,
            BETA_BALANCE_ENABLED,
            BETA_LOW_THRESHOLD,
            BETA_MIN_LOW_FRAC,
            BETA_MAX_HIGH_FRAC,
        )
    )


# --- 第三层：低波（静态兜底）---
VOL_LOOKBACK_DAYS = _env_int("DLV_VOL_LOOKBACK_DAYS", 60)
VOL_TRADING_DAYS_PER_YEAR = _env_int("DLV_VOL_TRADING_DAYS_PER_YEAR", 220)
MAX_ANNUALIZED_VOL_PCT = _env_float("DLV_MAX_ANNUALIZED_VOL_PCT", 50.0)
PRICE_HISTORY_BUFFER_DAYS = _env_int("DLV_PRICE_HISTORY_BUFFER_DAYS", 400)

# --- 排雷因子（硬性过滤，默认行业中性）---
RISK_FILTER_ENABLED = _env_bool("DLV_RISK_FILTER_ENABLED", True)
RISK_LOOKBACK_YEARS = _env_int("DLV_RISK_LOOKBACK_YEARS", 5)
MAX_ROE_VOLATILITY_RATIO = _env_float("DLV_MAX_ROE_VOLATILITY_RATIO", 0.45)
ROE_VOL_INDUSTRY_NEUTRAL = _env_bool("DLV_ROE_VOL_INDUSTRY_NEUTRAL", True)
MIN_DIVIDEND_YEARS = _env_int("DLV_MIN_DIVIDEND_YEARS", 5)
MIN_PAYOUT_RATIO_PCT = _env_float("DLV_MIN_PAYOUT_RATIO_PCT", 30.0)
MAX_PAYOUT_RATIO_PCT = _env_float("DLV_MAX_PAYOUT_RATIO_PCT", 70.0)
MAX_DEBT_RATIO_PCT = _env_float("DLV_MAX_DEBT_RATIO_PCT", 60.0)
DEBT_RATIO_INDUSTRY_NEUTRAL = _env_bool("DLV_DEBT_RATIO_INDUSTRY_NEUTRAL", True)
DEBT_RATIO_INDUSTRY_MARGIN_PCT = _env_float("DLV_DEBT_RATIO_INDUSTRY_MARGIN_PCT", 20.0)
MIN_INTEREST_COVERAGE = _env_float("DLV_MIN_INTEREST_COVERAGE", 3.0)
FILTER_RELAXATION_ENABLED = _env_bool("DLV_FILTER_RELAXATION_ENABLED", True)
# 排雷：默认软性评分扣减，替代硬性剔除（绝对质量底线仍硬过滤）
SOFT_RISK_SCORING_ENABLED = _env_bool("DLV_SOFT_RISK_SCORING_ENABLED", True)
RISK_PENALTY_ROE_VOL = _env_float("DLV_RISK_PENALTY_ROE_VOL", 2.5)
RISK_PENALTY_DIVIDEND_YEARS = _env_float("DLV_RISK_PENALTY_DIVIDEND_YEARS", 1.2)
RISK_PENALTY_PAYOUT = _env_float("DLV_RISK_PENALTY_PAYOUT", 1.5)
RISK_PENALTY_DEBT = _env_float("DLV_RISK_PENALTY_DEBT", 2.0)
RISK_PENALTY_INTEREST = _env_float("DLV_RISK_PENALTY_INTEREST", 1.8)
# 候选池动态保障：合格池至少 top_n × min_ratio，目标 top_n × target_ratio
CANDIDATE_POOL_MIN_RATIO = _env_float("DLV_CANDIDATE_POOL_MIN_RATIO", 1.5)
CANDIDATE_POOL_TARGET_RATIO = _env_float("DLV_CANDIDATE_POOL_TARGET_RATIO", 2.0)
ABS_MIN_ROE_PCT = _env_float("DLV_ABS_MIN_ROE_PCT", 8.0)
ABS_MAX_DEBT_RATIO_PCT = _env_float("DLV_ABS_MAX_DEBT_RATIO_PCT", 70.0)

# --- 动量（默认软性加分，非硬性剔除）---
MOMENTUM_HARD_FILTER_ENABLED = _env_bool("DLV_MOMENTUM_HARD_FILTER_ENABLED", False)
MOMENTUM_SCORE_WEIGHT = _env_float("DLV_MOMENTUM_SCORE_WEIGHT", 0.35)
MOMENTUM_MA_DAYS = _env_int("DLV_MOMENTUM_MA_DAYS", 250)
MOMENTUM_RETURN_DAYS = _env_int("DLV_MOMENTUM_RETURN_DAYS", 252)

# --- 全市场估值锚点（中证800 PE 分位）---
MARKET_VALUATION_ENABLED = _env_bool("DLV_MARKET_VALUATION_ENABLED", True)
MARKET_VALUATION_INDEX = _env_str("DLV_MARKET_VALUATION_INDEX", "000906")
MARKET_VALUATION_PE_LOOKBACK_DAYS = _env_int("DLV_MARKET_VALUATION_PE_LOOKBACK_DAYS", 2520)
MARKET_VALUATION_PE_TIGHT_PCT = _env_float("DLV_MARKET_VALUATION_PE_TIGHT_PCT", 80.0)
MARKET_VALUATION_PE_PAUSE_PCT = _env_float("DLV_MARKET_VALUATION_PE_PAUSE_PCT", 95.0)

# --- 指数化策略（H30269 风格默认）---
INDEX_STYLE_RANKING = _env_bool("DLV_INDEX_STYLE_RANKING", True)
INDEX_DIVIDEND_WEIGHTING = _env_bool("DLV_INDEX_DIVIDEND_WEIGHTING", True)
MARKET_VALUATION_PAUSE_BUYS_ENABLED = _env_bool("DLV_MARKET_VALUATION_PAUSE_BUYS_ENABLED", False)
# january=1月中旬调仓（避开12月除权密集期）；december=指数官方12月调样日
INDEX_ANNUAL_REBALANCE_TIMING = _env_str("DLV_INDEX_ANNUAL_REBALANCE_TIMING", "january").lower()
INDEX_JANUARY_REBALANCE_DAY = _env_int("DLV_INDEX_JANUARY_REBALANCE_DAY", 15)

# --- Beta 分散（消融保留：关闭后年化 -1.01pp）---
BETA_BALANCE_ENABLED = _env_bool("DLV_BETA_BALANCE_ENABLED", True)
BETA_BENCHMARK_CODE = _env_str("DLV_BETA_BENCHMARK_CODE", "000300")
BETA_LOOKBACK_DAYS = _env_int("DLV_BETA_LOOKBACK_DAYS", 252)
BETA_LOW_THRESHOLD = _env_float("DLV_BETA_LOW_THRESHOLD", 0.68)
BETA_MIN_LOW_FRAC = _env_float("DLV_BETA_MIN_LOW_FRAC", 0.45)  # 参数扫描+联合回测：45% 优于 35%
BETA_MAX_HIGH_FRAC = _env_float("DLV_BETA_MAX_HIGH_FRAC", 0.81)


# --- 止盈（分级：25% 静态可选 + 20% 后移动 15%）---
TAKE_PROFIT_ENABLED = _env_bool("DLV_TAKE_PROFIT_ENABLED", False)
TAKE_PROFIT_STATIC_ENABLED = _env_bool("DLV_TAKE_PROFIT_STATIC_ENABLED", False)
TAKE_PROFIT_PCT = _env_float("DLV_TAKE_PROFIT_PCT", 20.0)
TRAILING_STOP_ENABLED = _env_bool("DLV_TRAILING_STOP_ENABLED", False)
TRAILING_STOP_ACTIVATION_PCT = _env_float("DLV_TRAILING_STOP_ACTIVATION_PCT", 20.0)
TRAILING_STOP_FROM_PEAK_PCT = _env_float("DLV_TRAILING_STOP_FROM_PEAK_PCT", 15.0)
TRAILING_STOP_EXTENDED_PCT = _env_float("DLV_TRAILING_STOP_EXTENDED_PCT", 15.0)

# --- 止损（固定底线 + 波动动态）---
STOP_LOSS_ENABLED = _env_bool("DLV_STOP_LOSS_ENABLED", False)
STOP_LOSS_LOW_VOL_PCT = _env_float("DLV_STOP_LOSS_LOW_VOL_PCT", -12.0)
STOP_LOSS_HIGH_VOL_PCT = _env_float("DLV_STOP_LOSS_HIGH_VOL_PCT", -7.0)
STOP_LOSS_VOL_THRESHOLD_PCT = _env_float("DLV_STOP_LOSS_VOL_THRESHOLD_PCT", 25.0)
EMERGENCY_SELL_ENABLED = _env_bool("DLV_EMERGENCY_SELL_ENABLED", False)
EMERGENCY_SELL_DAILY_DROP_PCT = _env_float("DLV_EMERGENCY_SELL_DAILY_DROP_PCT", 8.0)
EMERGENCY_SELL_TWO_DAY_DROP_PCT = _env_float("DLV_EMERGENCY_SELL_TWO_DAY_DROP_PCT", 12.0)

# --- ATR 止损（与百分比止损并行，取更紧者）---
STOP_ATR_ENABLED = _env_bool("DLV_STOP_ATR_ENABLED", False)
STOP_ATR_MULTIPLIER = _env_float("DLV_STOP_ATR_MULTIPLIER", 3.0)
STOP_ATR_LOOKBACK = _env_int("DLV_STOP_ATR_LOOKBACK", 14)

# --- 条件回补（仓位回升后才加仓）---
CONDITIONAL_REBUY_ENABLED = _env_bool("DLV_CONDITIONAL_REBUY_ENABLED", False)
CONDITIONAL_REBUY_MIN_POSITION_SCALE = _env_float("DLV_CONDITIONAL_REBUY_MIN_POSITION_SCALE", 0.85)

# --- 动量卖出 + 缓冲带观察期（波动自适应）---
MOMENTUM_SELL_ENABLED = _env_bool("DLV_MOMENTUM_SELL_ENABLED", False)
MOMENTUM_SELL_MA_DAYS = _env_int("DLV_MOMENTUM_SELL_MA_DAYS", 200)
MOMENTUM_SELL_RANK_THRESHOLD = _env_int("DLV_MOMENTUM_SELL_RANK_THRESHOLD", 20)
SELL_GRACE_PERIOD_ENABLED = _env_bool("DLV_SELL_GRACE_PERIOD_ENABLED", False)
SELL_GRACE_PERIOD_DAYS = _env_int("DLV_SELL_GRACE_PERIOD_DAYS", 3)
GRACE_VOL_ADAPTIVE_ENABLED = _env_bool("DLV_GRACE_VOL_ADAPTIVE_ENABLED", True)
GRACE_REBOUND_RESET_ENABLED = _env_bool("DLV_GRACE_REBOUND_RESET_ENABLED", True)
GRACE_EARLY_SELL_DOWN_DAYS = _env_int("DLV_GRACE_EARLY_SELL_DOWN_DAYS", 3)
GRACE_VOL_HIGH_THRESHOLD_PCT = _env_float("DLV_GRACE_VOL_HIGH_THRESHOLD_PCT", 32.0)
GRACE_PERIOD_DAYS_HIGH_VOL = _env_int("DLV_GRACE_PERIOD_DAYS_HIGH_VOL", 5)
GRACE_PERIOD_DAYS_LOW_VOL = _env_int("DLV_GRACE_PERIOD_DAYS_LOW_VOL", 3)


def _parse_grace_vol_tiers() -> tuple[tuple[float, int], ...]:
    raw = os.environ.get("DLV_GRACE_VOL_TIER_ADJUSTMENTS", "30:3,25:2,20:1")
    tiers: list[tuple[float, int]] = []
    for part in raw.split(","):
        if ":" not in part:
            continue
        thr, add = part.split(":", 1)
        tiers.append((float(thr.strip()), int(add.strip())))
    return tuple(sorted(tiers, key=lambda x: -x[0]))


GRACE_VOL_TIER_ADJUSTMENTS = _parse_grace_vol_tiers()

# --- 滑点（动态：波动 + 成交占流动性比例）---
SLIPPAGE_RATE = _env_float("DLV_SLIPPAGE_RATE", 0.001)
SLIPPAGE_DYNAMIC_ENABLED = _env_bool("DLV_SLIPPAGE_DYNAMIC_ENABLED", True)
SLIPPAGE_BASE_RATE = _env_float("DLV_SLIPPAGE_BASE_RATE", 0.0005)
SLIPPAGE_MAX_RATE = _env_float("DLV_SLIPPAGE_MAX_RATE", 0.0025)
SLIPPAGE_PARTICIPATION_MULT = _env_float("DLV_SLIPPAGE_PARTICIPATION_MULT", 0.15)
SLIPPAGE_ADV_BASE_CNY = _env_float("DLV_SLIPPAGE_ADV_BASE_CNY", 8_000_000.0)

# --- 市场状态 / 前置仓位（波动预警 + 市场宽度 + 波动率目标）---
MARKET_REGIME_ENABLED = _env_bool("DLV_MARKET_REGIME_ENABLED", False)
BEAR_VOL_THRESHOLD_PCT = _env_float("DLV_BEAR_VOL_THRESHOLD_PCT", 30.0)
BEAR_VOL_USE_PERCENTILE = _env_bool("DLV_BEAR_VOL_USE_PERCENTILE", True)
BEAR_VOL_PERCENTILE_THRESHOLD = _env_float("DLV_BEAR_VOL_PERCENTILE_THRESHOLD", 0.75)
BEAR_VOL_PERCENTILE_LOOKBACK = _env_int("DLV_BEAR_VOL_PERCENTILE_LOOKBACK", 756)
BEAR_VOL_MIN_SAMPLES = _env_int("DLV_BEAR_VOL_MIN_SAMPLES", 60)
BEAR_POSITION_SCALE = _env_float("DLV_BEAR_POSITION_SCALE", 0.65)
BEAR_MAX_VOL_CEILING_PCT = _env_float("DLV_BEAR_MAX_VOL_CEILING_PCT", 38.0)

MARKET_BREADTH_ENABLED = _env_bool("DLV_MARKET_BREADTH_ENABLED", False)
BREADTH_BELOW_MA250_THRESHOLD_PCT = _env_float("DLV_BREADTH_BELOW_MA250_THRESHOLD_PCT", 70.0)
BREADTH_BELOW_MA250_SCALE = _env_float("DLV_BREADTH_BELOW_MA250_SCALE", 0.70)

VOL_TARGET_ENABLED = _env_bool("DLV_VOL_TARGET_ENABLED", True)
VOL_TARGET_PCT = _env_float("DLV_VOL_TARGET_PCT", 20.0)


def _parse_vix_tiers() -> tuple[tuple[float, float], ...]:
    raw = os.environ.get("DLV_VIX_PROXY_POSITION_TIERS", "35:0.5,30:0.65,25:0.8")
    tiers: list[tuple[float, float]] = []
    for part in raw.split(","):
        if ":" not in part:
            continue
        thr, scale = part.split(":", 1)
        tiers.append((float(thr.strip()), float(scale.strip())))
    return tuple(sorted(tiers, key=lambda x: -x[0]))


VIX_PROXY_POSITION_TIERS = _parse_vix_tiers()

# --- 第四层：排名打分 ---
YIELD_RANK_WEIGHT = _env_float("DLV_YIELD_RANK_WEIGHT", 1.68)
VOL_RANK_WEIGHT = _env_float("DLV_VOL_RANK_WEIGHT", 0.54)
QUALITY_MOMENTUM_WEIGHT = _env_float("DLV_QUALITY_MOMENTUM_WEIGHT", 0.1)
DYNAMIC_WEIGHT_ENABLED = _env_bool("DLV_DYNAMIC_WEIGHT_ENABLED", False)
YIELD_WEIGHT_BASE = _env_float("DLV_YIELD_WEIGHT_BASE", 1.0)
VOL_WEIGHT_BASE = _env_float("DLV_VOL_WEIGHT_BASE", 0.5)
BOND_YIELD_REF_PCT = _env_float("DLV_BOND_YIELD_REF_PCT", 2.5)
MARKET_VOL_REF_PCT = _env_float("DLV_MARKET_VOL_REF_PCT", 25.0)
YIELD_WEIGHT_BOND_SENS = _env_float("DLV_YIELD_WEIGHT_BOND_SENS", 0.15)
VOL_WEIGHT_MARKET_SENS = _env_float("DLV_VOL_WEIGHT_MARKET_SENS", 0.01)

# --- 第五层：调仓（指数化默认：10 只 / 年度调样 / index_rules）---
TOP_N_BUY = _env_int("DLV_TOP_N_BUY", 10)
TOP_N_MIN_BUY = _env_int("DLV_TOP_N_MIN_BUY", 5)
SELL_RANK_MULTIPLIER = _env_float("DLV_SELL_RANK_MULTIPLIER", 2.5)
SELL_RANK_BUFFER = _env_int(
    "DLV_SELL_RANK_BUFFER",
    max(int(round(TOP_N_BUY * SELL_RANK_MULTIPLIER)), TOP_N_BUY + 1),
)

# --- 行业分散 / 个股上限 ---
INDUSTRY_CAP_ENABLED = _env_bool("DLV_INDUSTRY_CAP_ENABLED", True)
MAX_INDUSTRY_WEIGHT = _env_float("DLV_MAX_INDUSTRY_WEIGHT", 0.20)
MAX_SINGLE_STOCK_WEIGHT = _env_float("DLV_MAX_SINGLE_STOCK_WEIGHT", 0.08)
MAX_DEFENSIVE_INDUSTRY_WEIGHT = _env_float("DLV_MAX_DEFENSIVE_INDUSTRY_WEIGHT", 0.45)
MAX_TOP3_INDUSTRY_WEIGHT = _env_float("DLV_MAX_TOP3_INDUSTRY_WEIGHT", 0.50)
DEFENSIVE_INDUSTRY_KEYWORDS = tuple(
    k.strip()
    for k in os.environ.get(
        "DLV_DEFENSIVE_INDUSTRY_KEYWORDS",
        "银行,公用事业,煤炭,钢铁,交通,电力,石化,燃气,铁路,港口,水务,环保",
    ).split(",")
    if k.strip()
)
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
BACKTEST_REBALANCE_MODE = _env_str(
    "DLV_BACKTEST_REBALANCE_MODE", "index_annual"
).lower()  # index_annual | monthly | quarterly_report | fixed_days
BACKTEST_REBALANCE_DAYS = _env_int("DLV_BACKTEST_REBALANCE_DAYS", 30)
BACKTEST_MIN_HOLD_DAYS = _env_int("DLV_BACKTEST_MIN_HOLD_DAYS", 0)

# --- 调出逻辑 ---
# rank_buffer：跌出 sell_rank 缓冲带即卖
# index_rules：H30269 风格，仅股息率/排雷/流动性等硬门槛不达标才卖（默认）
SELL_MODE = _env_str("DLV_SELL_MODE", "index_rules").lower()
# index_rules 下在调仓日之间逐日检查止损/紧急卖出（应对年内暴跌）
INDEX_RULES_DAILY_RISK_ENABLED = _env_bool("DLV_INDEX_RULES_DAILY_RISK_ENABLED", False)
INDEX_RETENTION_MIN_DIVIDEND_YIELD_PCT = _env_float("DLV_INDEX_RETENTION_MIN_DIVIDEND_YIELD_PCT", 0.5)

def _env_kline_fq(name: str, default: str | None) -> str | None:
    """stockdb fq：None/空/bfq=不复权；qfq/hfq=复权。"""
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in {"none", "bfq", "raw", "unadjusted", "0"}:
        return None
    return v


BACKTEST_KLINE_FQ = _env_kline_fq("DLV_BACKTEST_KLINE_FQ", "qfq")
BACKTEST_DIVIDEND_CASH = _env_bool("DLV_BACKTEST_DIVIDEND_CASH", True)
BACKTEST_INITIAL_CAPITAL = _env_float("DLV_BACKTEST_INITIAL_CAPITAL", 100_000.0)
BACKTEST_YEARS = _env_int("DLV_BACKTEST_YEARS", 10)
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
    return max(int(round(top_n * SELL_RANK_MULTIPLIER)), top_n + 1)
