"""项目公共配置：默认值、环境变量与 push.env 覆盖。"""

import os
from functools import lru_cache
from pathlib import Path

from data_sources import (
    BOND_YIELD_URL as _DEFAULT_BOND_YIELD_URL,
    CSINDEX_INDICATOR_BASE_URL as _DEFAULT_CSINDEX_INDICATOR_BASE_URL,
    FRED_CSV_BASE_URL as _DEFAULT_FRED_CSV_BASE_URL,
    FRED_NASDAQ100_SERIES as _DEFAULT_FRED_NASDAQ100_SERIES,
    INDEX_PERF_URL as _DEFAULT_INDEX_PERF_URL,
    SERVERCHAN_API_URL as _DEFAULT_SERVERCHAN_API_URL,
    SHILLER_IE_DATA_URL as _DEFAULT_SHILLER_IE_DATA_URL,
    TENCENT_QUOTE_URL as _DEFAULT_TENCENT_QUOTE_URL,
)

PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = PROJECT_DIR / "push.env"
LOGS_DIR = PROJECT_DIR / "logs"
DATA_CACHE_DIR = PROJECT_DIR / "cache"
US_DATA_CACHE_DIR = DATA_CACHE_DIR / "us"
BACKTEST_OUTPUT_DIR = PROJECT_DIR / "output" / "backtest"
BACKTEST_PRESENT_LABEL = "inception_present"

INDICES = [
    {"code": "H30269", "name": "中证红利低波动"},
]

# 红利价格指数 → 中证全收益指数（分红再投资，同源 csindex-home/perf API）
DIVIDEND_TOTAL_RETURN_INDEX = {
    "H30269": "H20269",
}


def get_dividend_total_return_code(index_code):
    """红利价格指数对应的全收益指数代码（含分红再投资）。"""
    return DIVIDEND_TOTAL_RETURN_INDEX.get(index_code)

ZZ1000_INDEX = {"code": "000852", "name": "中证1000"}
KC50_INDEX = {"code": "000688", "name": "科创50"}
CYB_INDEX = {"code": "399006", "name": "创业板指"}
NDX_INDEX = {"code": "NDX", "name": "纳斯达克100"}
NDX_MARKET_DATA_START = "2010-01-01"
SPX_INDEX = {"code": "SPX", "name": "标普500"}
SPX_MARKET_DATA_START = "2013-01-01"

CN_BROAD_INDICES = [
    ZZ1000_INDEX,
    KC50_INDEX,
]
US_INDEX_KEYS = ("ndx", "spx")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

BOND_YIELD_FIELD = "EMM00166466"
BOND_YIELD_PARAMS = {
    "type": "RPTA_WEB_TREASURYYIELD",
    "sty": "ALL",
    "st": "SOLAR_DATE",
    "sr": "-1",
    "token": "894050c76af8597a853f5b408b759f5d",
    "p": "1",
    "ps": "1",
    "pageNo": "1",
    "pageNum": "1",
}

ENV_BOOL_TRUE = {"1", "true", "yes", "on"}


def _load_env_files():
    """将 push.env 中的配置写入环境变量（不覆盖已有环境变量）。"""
    for path in [CONFIG_FILE]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _env_str(name, default):
    return os.environ.get(name, default)


def _env_float(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_int(name, default):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float_any(names, default):
    """按顺序读取环境变量，支持新旧变量名兼容。"""
    if isinstance(names, str):
        names = (names,)
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return float(value)
    return default


def _env_int_any(names, default):
    if isinstance(names, str):
        names = (names,)
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return int(value)
    return default


def _env_bool(name, default=True):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ENV_BOOL_TRUE


def _build_annual_investment_budget_by_year():
    """从环境变量 ANNUAL_INVESTMENT_BUDGET_{年份} 读取各年覆盖值。"""
    out = {}
    for year in range(2015, 2036):
        value = os.environ.get(f"ANNUAL_INVESTMENT_BUDGET_{year}")
        if value is not None and value != "":
            out[year] = float(value)
    return out


_load_env_files()

# --- 回测/实盘单次买入金额（元）---
# 默认各指数统一基准 100；实际买入 = 基准 × 当日涨跌系数（见 buy_amount_change.py）
DIVIDEND_BUY_AMOUNT = _env_float("DIVIDEND_BUY_AMOUNT", 100)
CN_BROAD_BUY_AMOUNT = _env_float("CN_BROAD_BUY_AMOUNT", 100)
BACKTEST_OTHER_BUY_AMOUNT = _env_float("BACKTEST_OTHER_BUY_AMOUNT", 100)

# --- 投入预算：剩余可用额度（实盘展示用；默认不缩放回测单笔金额）---
REMAINING_INVESTMENT_BUDGET = _env_float_any(
    ("REMAINING_INVESTMENT_BUDGET", "ANNUAL_INVESTMENT_BUDGET"),
    50_000,
)
ANNUAL_INVESTMENT_TARGET = _env_float("ANNUAL_INVESTMENT_TARGET", 0)
ANNUAL_INVESTMENT_BUDGET = REMAINING_INVESTMENT_BUDGET
BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET = _env_float(
    "BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET", 72_577
)
ANNUAL_INVESTMENT_BUDGET_ENABLED = _env_bool("ANNUAL_INVESTMENT_BUDGET_ENABLED", True)
ANNUAL_INVESTMENT_BUDGET_BY_YEAR = _build_annual_investment_budget_by_year()

# --- 买入金额：按当日涨跌比例缩放（跌多买多，反弹少买）---
# amount = base * clamp(1 - sensitivity * daily_change_pct, min_mult, max_mult)
BUY_AMOUNT_CHANGE_SCALE_ENABLED = _env_bool("BUY_AMOUNT_CHANGE_SCALE_ENABLED", True)
BUY_AMOUNT_CHANGE_SENSITIVITY = _env_float("BUY_AMOUNT_CHANGE_SENSITIVITY", 10.0)
BUY_AMOUNT_CHANGE_MIN_MULT = _env_float("BUY_AMOUNT_CHANGE_MIN_MULT", 0.5)
BUY_AMOUNT_CHANGE_MAX_MULT = _env_float("BUY_AMOUNT_CHANGE_MAX_MULT", 2.0)
BUY_AMOUNT_POSITION_ALLOC_ENABLED = _env_bool(
    "BUY_AMOUNT_POSITION_ALLOC_ENABLED", True
)
BUY_AMOUNT_RANKING_ENABLED = _env_bool("BUY_AMOUNT_RANKING_ENABLED", False)
BUY_AMOUNT_RETURN_MAX = _env_bool("BUY_AMOUNT_RETURN_MAX", True)

# 各指数基准单次买入（元）；可用 {代码}_BUY_AMOUNT 覆盖
BUY_AMOUNT_DEFAULT = _env_float("BUY_AMOUNT_DEFAULT", 100)
BUY_AMOUNT_BASE_BY_CODE = {
    "NDX": _env_float("NDX_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "SPX": _env_float("SPX_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "399006": _env_float("399006_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "H30269": _env_float("H30269_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "000688": _env_float("000688_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
    "000852": _env_float("000852_BUY_AMOUNT", BUY_AMOUNT_DEFAULT),
}


def _env_buy_amount_for_code(code, default):
    return _env_float_any((f"{code}_BUY_AMOUNT", f"BACKTEST_{code}_BUY_AMOUNT"), default)


def get_buy_amount_reference(index_code):
    """参考基准金额（未按涨跌缩放）。"""
    if BUY_AMOUNT_POSITION_ALLOC_ENABLED:
        from buy_amount_allocation import get_position_allocation

        alloc = get_position_allocation()
        ref = alloc["reference_by_code"].get(index_code)
        if ref is not None:
            return _env_buy_amount_for_code(index_code, float(ref))
    if BUY_AMOUNT_RANKING_ENABLED:
        from buy_amount_ranking import get_ranking_allocation

        alloc = get_ranking_allocation()
        ref = alloc["reference_by_code"].get(index_code)
        if ref is not None:
            return _env_buy_amount_for_code(index_code, float(ref))
    default = BUY_AMOUNT_BASE_BY_CODE.get(index_code, BUY_AMOUNT_DEFAULT)
    return _env_buy_amount_for_code(index_code, default)


def is_index_recommended(index_code):
    """当前是否参与买入额度分配（有正数基准金额即视为可买）。"""
    return get_buy_amount_reference(index_code) > 0


def get_buy_amount_base(index_code, year=None):
    """单只指数基准单次买入金额（元）；启用年度预算时按当年总投入缩放。"""
    ref = get_buy_amount_reference(index_code)
    if BUY_AMOUNT_POSITION_ALLOC_ENABLED:
        return ref
    if not ANNUAL_INVESTMENT_BUDGET_ENABLED:
        return ref
    from buy_amount_budget import get_scaled_buy_amount_base

    return get_scaled_buy_amount_base(index_code, year)


def get_backtest_buy_amount(index_code, amounts=None):
    """读取单只指数单次买入金额（元）；0 表示不买入。"""
    if amounts is not None:
        by_code = amounts.get("by_code")
        if by_code is not None and index_code in by_code:
            return float(by_code[index_code])
        if amounts.get("unified"):
            return float(amounts["dividend"])
        if index_code in {i["code"] for i in INDICES}:
            return float(amounts.get("dividend", DIVIDEND_BUY_AMOUNT))
        if index_code in {i["code"] for i in CN_BROAD_INDICES}:
            return float(amounts.get("cn_broad", CN_BROAD_BUY_AMOUNT))
        return float(amounts.get("other", BACKTEST_OTHER_BUY_AMOUNT))
    return get_buy_amount_base(index_code)


def get_chart_buy_amount(index_code, amounts=None):
    """HTML 图表用单次买入金额。"""
    amt = get_backtest_buy_amount(index_code, amounts)
    if amt > 0:
        return amt
    if amounts is not None:
        by_code = amounts.get("by_code")
        if by_code is not None:
            return 0.0
    return get_buy_amount_base(index_code)


def _static_buy_amount_by_code():
    """回测用固定基准金额（不读取收益率排名，避免未来函数）。"""
    from buy_amount_config import ALL_BUY_INDEX_CODES

    return {
        code: _env_buy_amount_for_code(
            code, BUY_AMOUNT_BASE_BY_CODE.get(code, BUY_AMOUNT_DEFAULT)
        )
        for code in ALL_BUY_INDEX_CODES
    }


def resolve_backtest_amounts(
    unified_amount=None,
    return_max_mode=None,
    ranking_mode=None,
    change_scale=None,
    tier_enabled=None,
    portfolio_mode=False,
    panels=None,
    position_alloc_mode=None,
):
    """回测买入金额。默认位置分配 + 年度预算 + 涨跌缩放。"""
    from config import BUY_AMOUNT_POSITION_ALLOC_ENABLED

    if change_scale is None:
        if tier_enabled is not None:
            change_scale = bool(tier_enabled) and BUY_AMOUNT_CHANGE_SCALE_ENABLED
        else:
            change_scale = BUY_AMOUNT_CHANGE_SCALE_ENABLED

    def _pack(by_code=None, **extra):
        base = {
            "dividend": DIVIDEND_BUY_AMOUNT,
            "cn_broad": CN_BROAD_BUY_AMOUNT,
            "other": BACKTEST_OTHER_BUY_AMOUNT,
            "unified": False,
            "portfolio": False,
            "ranking": False,
            "position_alloc": False,
            "return_max": False,
            "by_code": by_code,
            "change_scale": change_scale,
            "tier_scheme": None,
            "tier_normalize": False,
            "annual_budget": ANNUAL_INVESTMENT_BUDGET_ENABLED,
            "annual_budget_default": ANNUAL_INVESTMENT_BUDGET,
            "reference_annual_budget": BUY_AMOUNT_REFERENCE_ANNUAL_BUDGET,
        }
        base.update(extra)
        return base

    if unified_amount is not None and unified_amount > 0:
        return _pack(
            unified=True,
            dividend=float(unified_amount),
            cn_broad=float(unified_amount),
            other=float(unified_amount),
        )
    if ranking_mode:
        from buy_amount_ranking import get_ranking_allocation

        alloc = get_ranking_allocation()
        return _pack(
            by_code=dict(alloc["by_code"]),
            ranking=True,
            reference_by_code=dict(alloc["reference_by_code"]),
            excluded_codes=alloc["excluded_codes"],
            ranking_rows=alloc["rows"],
            ranking_as_of=alloc.get("as_of"),
        )
    use_position = (
        BUY_AMOUNT_POSITION_ALLOC_ENABLED
        if position_alloc_mode is None
        else bool(position_alloc_mode)
    )
    if use_position:
        from buy_amount_allocation import compute_backtest_position_allocation

        alloc = compute_backtest_position_allocation(panels=panels)
        return _pack(
            by_code=dict(alloc["by_code"]),
            position_alloc=True,
            reference_by_code=dict(alloc["reference_by_code"]),
            allocation_rows=alloc.get("rows"),
            allocation_as_of=alloc.get("as_of"),
        )
    if return_max_mode is None:
        return_max_mode = BUY_AMOUNT_RETURN_MAX
    if return_max_mode:
        by_code = _static_buy_amount_by_code()
        return _pack(
            by_code=by_code,
            return_max=True,
            reference_by_code=dict(by_code),
        )
    return _pack()


def format_backtest_amount_note(amounts):
    """Markdown/控制台用的买入金额说明。"""
    if amounts is None:
        return "仅统计次数"
    change = amounts.get("change_scale", BUY_AMOUNT_CHANGE_SCALE_ENABLED)
    change_suffix = (
        f" + 涨跌缩放（敏感度 {BUY_AMOUNT_CHANGE_SENSITIVITY:g}，"
        f"{BUY_AMOUNT_CHANGE_MIN_MULT:g}–{BUY_AMOUNT_CHANGE_MAX_MULT:g}×）"
        if change
        else ""
    )
    if amounts.get("unified"):
        return f"每次买入 **{amounts['dividend']:.0f}** 元{change_suffix}"
    if amounts.get("ranking") and amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        from buy_amount_ranking import format_ranking_note

        head = format_ranking_note(
            {
                "rows": amounts.get("ranking_rows") or [],
                "excluded_codes": amounts.get("excluded_codes") or frozenset(),
                "exclude_bottom_n": len(amounts.get("excluded_codes") or ()),
                "as_of": amounts.get("ranking_as_of"),
            }
        )
        return f"{head}{change_suffix}：{'；'.join(parts)}"
    if amounts.get("position_alloc") and amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        from buy_amount_allocation import format_allocation_note

        head = "位置分配（回测按历史买入日低位程度）"
        if amounts.get("annual_budget"):
            from buy_amount_budget import format_annual_budget_note

            head = format_annual_budget_note() + "；" + head
        return head + change_suffix + "：" + "；".join(parts)
    if amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        head = "固定基准金额"
        if amounts.get("annual_budget"):
            from buy_amount_budget import format_annual_budget_note

            head = format_annual_budget_note() + "；" + head
        return head + change_suffix + "：" + "；".join(parts)
    return f"模块默认金额{change_suffix}"

# =============================================================================
# 各指数买入 / 卖出信号阈值（可通过 push.env 覆盖）
# =============================================================================

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
# 滚动区间位置（默认近 252 交易日 ≈ 1 年）：0=窗口内最低、1=窗口内最高
BUY_RANGE_LOOKBACK_DAYS = _env_int("BUY_RANGE_LOOKBACK_DAYS", 252)
BUY_MAX_YEAR_RANGE_PCT = _env_float("BUY_MAX_YEAR_RANGE_PCT", 0.58)
BUY_NEAR_YEAR_LOW_RANGE_PCT = _env_float("BUY_NEAR_YEAR_LOW_RANGE_PCT", 0.20)
BUY_NEAR_YEAR_LOW_SPREAD_RELAX = _env_float("BUY_NEAR_YEAR_LOW_SPREAD_RELAX", 10)
BUY_NEAR_YEAR_LOW_PE_RELAX = _env_float("BUY_NEAR_YEAR_LOW_PE_RELAX", 12)
BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX = _env_float("BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX", 0.04)
BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT = _env_float(
    "BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT", 0.12
)
BUY_MID_RANGE_POSITION_PCT = _env_float("BUY_MID_RANGE_POSITION_PCT", 0.45)
BUY_MID_RANGE_MAX_ABOVE_LOW_PCT = _env_float("BUY_MID_RANGE_MAX_ABOVE_LOW_PCT", 0.06)
# 均线趋势过滤：MA200 斜率（默认 60 日变化率）
BUY_TREND_MA_DAYS = _env_int("BUY_TREND_MA_DAYS", 200)
BUY_TREND_SLOPE_LOOKBACK_DAYS = _env_int("BUY_TREND_SLOPE_LOOKBACK_DAYS", 60)
BUY_TREND_MIN_MA_SLOPE_PCT = _env_float("BUY_TREND_MIN_MA_SLOPE_PCT", -0.02)
BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.10
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


# --- 创业板指（399006）---
# 买入：PEG(近5年增速) + 价格位置 + MA 趋势
CYB_EXPECTED_GROWTH = _env_float("CYB_EXPECTED_GROWTH", 0.3906)
# 兜底固定增速；启用自动时优先用面板滚动 5 年盈利 CAGR（close/PE）
CYB_HISTORICAL_GROWTH = _env_float("CYB_HISTORICAL_GROWTH", 0.1663)
CYB_HISTORICAL_GROWTH_AUTO = _env_bool("CYB_HISTORICAL_GROWTH_AUTO", True)
CYB_HISTORICAL_GROWTH_YEARS = _env_int("CYB_HISTORICAL_GROWTH_YEARS", 5)
CYB_HISTORICAL_GROWTH_MIN_DAYS = _env_int("CYB_HISTORICAL_GROWTH_MIN_DAYS", 756)
# 自动 CAGR 下限：默认等于历史兜底，避免自动化把 PEG 收得比原策略更严；
# 若希望按真实低增速收紧，可将 floor 调低（如 0.10）
CYB_HISTORICAL_GROWTH_FLOOR = _env_float("CYB_HISTORICAL_GROWTH_FLOOR", 0.1663)
CYB_ROE_AVG = _env_float("CYB_ROE_AVG", 0.1229)
# 创业板指：2016-2025 夏普偏低(0.41)且回撤大，收紧买入
CYB_BUY_PEG_HIST_MAX = _env_float("CYB_BUY_PEG_HIST_MAX", 2.2)
CYB_PERCENTILE_WINDOW = _env_int("CYB_PERCENTILE_WINDOW", 2520)
CYB_DIV_PERCENTILE_WINDOW = _env_int("CYB_DIV_PERCENTILE_WINDOW", 1260)
CYB_PERCENTILE_MIN_DAYS = _env_int("CYB_PERCENTILE_MIN_DAYS", 120)
CYB_BUY_MAX_ABOVE_LOW_PCT = _env_float("CYB_BUY_MAX_ABOVE_LOW_PCT", 0.06)
CYB_BUY_LOW_LOOKBACK_DAYS = _env_int("CYB_BUY_LOW_LOOKBACK_DAYS", 90)
CYB_BUY_HIGH_LOOKBACK_DAYS = _env_int("CYB_BUY_HIGH_LOOKBACK_DAYS", 252)
CYB_BUY_MID_RANGE_POSITION_PCT = _env_float(
    "CYB_BUY_MID_RANGE_POSITION_PCT", 0.45
)
CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT = _env_float(
    "CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT", 0.06
)
CYB_BUY_TREND_MA_DAYS = _env_int("CYB_BUY_TREND_MA_DAYS", BUY_TREND_MA_DAYS)
CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS = _env_int(
    "CYB_BUY_TREND_SLOPE_LOOKBACK_DAYS", BUY_TREND_SLOPE_LOOKBACK_DAYS
)
CYB_BUY_TREND_MIN_MA_SLOPE_PCT = _env_float(
    "CYB_BUY_TREND_MIN_MA_SLOPE_PCT", -0.015
)
CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.08
)
CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT = _env_float(
    "CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT", BUY_NEAR_YEAR_LOW_RANGE_PCT
)

# --- 纳斯达克 100（NDX）---
# 买入：Forward PE 分位 + 10Y 利率分位 + 年区间位置 + MA 趋势
NDX_FORWARD_PE_URL = _env_str(
    "NDX_FORWARD_PE_URL",
    "https://historyofmarket.com/api/ndx/forward-pe.json",
)
NDX_DIVIDEND_PROXY_SYMBOL = _env_str("NDX_DIVIDEND_PROXY_SYMBOL", "QQQ")
NDX_EXPECTED_GROWTH = _env_float("NDX_EXPECTED_GROWTH", 0.0) or None
NDX_FALLBACK_EXPECTED_GROWTH = _env_float("NDX_FALLBACK_EXPECTED_GROWTH", 0.19)
NDX_BUY_FORWARD_PE_PERCENTILE_MAX = _env_float(
    "NDX_BUY_FORWARD_PE_PERCENTILE_MAX", 87
)
NDX_BUY_RATE_PERCENTILE_MAX = _env_float("NDX_BUY_RATE_PERCENTILE_MAX", 99)
NDX_BUY_RATE_SLOPE_LOOKBACK_DAYS = _env_int("NDX_BUY_RATE_SLOPE_LOOKBACK_DAYS", 21)
NDX_BUY_RATE_MAX_SLOPE = _env_float("NDX_BUY_RATE_MAX_SLOPE", 0.004)
NDX_HISTORY_YEARS = _env_int("NDX_HISTORY_YEARS", 10)
NDX_PERCENTILE_WINDOW = _env_int("NDX_PERCENTILE_WINDOW", 120)
NDX_PERCENTILE_MIN_DAYS = _env_int("NDX_PERCENTILE_MIN_DAYS", 24)
NDX_DAILY_PERCENTILE_WINDOW = _env_int("NDX_DAILY_PERCENTILE_WINDOW", 2520)
NDX_DAILY_PERCENTILE_MIN_DAYS = _env_int("NDX_DAILY_PERCENTILE_MIN_DAYS", 252)
NDX_FRED_NETWORK_TIMEOUT = _env_int("NDX_FRED_NETWORK_TIMEOUT", 10)
NDX_BUY_MAX_ABOVE_LOW_PCT = _env_float("NDX_BUY_MAX_ABOVE_LOW_PCT", 0.38)
NDX_BUY_LOW_LOOKBACK_DAYS = _env_int("NDX_BUY_LOW_LOOKBACK_DAYS", 90)
NDX_BUY_HIGH_LOOKBACK_DAYS = _env_int("NDX_BUY_HIGH_LOOKBACK_DAYS", 252)
NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float(
    "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT", 0.0
)
NDX_BUY_MAX_YEAR_RANGE_PCT = _env_float("NDX_BUY_MAX_YEAR_RANGE_PCT", 0.68)
NDX_BUY_MID_RANGE_POSITION_PCT = _env_float(
    "NDX_BUY_MID_RANGE_POSITION_PCT", 0.45
)
NDX_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT = _env_float(
    "NDX_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT", 0.10
)
NDX_BUY_TREND_MA_DAYS = _env_int("NDX_BUY_TREND_MA_DAYS", BUY_TREND_MA_DAYS)
NDX_BUY_TREND_SLOPE_LOOKBACK_DAYS = _env_int(
    "NDX_BUY_TREND_SLOPE_LOOKBACK_DAYS", BUY_TREND_SLOPE_LOOKBACK_DAYS
)
NDX_BUY_TREND_MIN_MA_SLOPE_PCT = _env_float(
    "NDX_BUY_TREND_MIN_MA_SLOPE_PCT", -0.02
)
NDX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "NDX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.30
)
NDX_BUY_NEAR_YEAR_LOW_RANGE_PCT = _env_float(
    "NDX_BUY_NEAR_YEAR_LOW_RANGE_PCT", BUY_NEAR_YEAR_LOW_RANGE_PCT
)
NDX_BUY_NEAR_YEAR_LOW_PE_RELAX = _env_float(
    "NDX_BUY_NEAR_YEAR_LOW_PE_RELAX", BUY_NEAR_YEAR_LOW_PE_RELAX
)
NDX_BUY_NEAR_YEAR_LOW_RATE_RELAX = _env_float(
    "NDX_BUY_NEAR_YEAR_LOW_RATE_RELAX", 12
)

# --- 标普 500（SPX，逻辑参考纳斯达克 100）---
SPX_FORWARD_PE_URL = _env_str(
    "SPX_FORWARD_PE_URL",
    "https://historyofmarket.com/api/sp500/forward-pe.json",
)
SPX_DIVIDEND_PROXY_SYMBOL = _env_str("SPX_DIVIDEND_PROXY_SYMBOL", "SPY")
SPX_EXPECTED_GROWTH = _env_float("SPX_EXPECTED_GROWTH", 0.0) or None
SPX_FALLBACK_EXPECTED_GROWTH = _env_float("SPX_FALLBACK_EXPECTED_GROWTH", 0.10)
SPX_BUY_FORWARD_PE_PERCENTILE_MAX = _env_float(
    "SPX_BUY_FORWARD_PE_PERCENTILE_MAX", 87
)
SPX_BUY_RATE_PERCENTILE_MAX = _env_float("SPX_BUY_RATE_PERCENTILE_MAX", 99)
SPX_BUY_RATE_SLOPE_LOOKBACK_DAYS = _env_int("SPX_BUY_RATE_SLOPE_LOOKBACK_DAYS", 21)
SPX_BUY_RATE_MAX_SLOPE = _env_float("SPX_BUY_RATE_MAX_SLOPE", 0.004)
SPX_HISTORY_YEARS = _env_int("SPX_HISTORY_YEARS", 10)
SPX_PERCENTILE_WINDOW = _env_int("SPX_PERCENTILE_WINDOW", 120)
SPX_PERCENTILE_MIN_DAYS = _env_int("SPX_PERCENTILE_MIN_DAYS", 24)
SPX_DAILY_PERCENTILE_WINDOW = _env_int("SPX_DAILY_PERCENTILE_WINDOW", 2520)
SPX_DAILY_PERCENTILE_MIN_DAYS = _env_int("SPX_DAILY_PERCENTILE_MIN_DAYS", 252)
SPX_FRED_NETWORK_TIMEOUT = _env_int("SPX_FRED_NETWORK_TIMEOUT", 10)
SPX_BUY_MAX_ABOVE_LOW_PCT = _env_float("SPX_BUY_MAX_ABOVE_LOW_PCT", 0.35)
SPX_BUY_LOW_LOOKBACK_DAYS = _env_int("SPX_BUY_LOW_LOOKBACK_DAYS", 90)
SPX_BUY_HIGH_LOOKBACK_DAYS = _env_int("SPX_BUY_HIGH_LOOKBACK_DAYS", 252)
SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float(
    "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT", 0.0
)
SPX_BUY_MAX_YEAR_RANGE_PCT = _env_float("SPX_BUY_MAX_YEAR_RANGE_PCT", 0.68)
SPX_BUY_MID_RANGE_POSITION_PCT = _env_float(
    "SPX_BUY_MID_RANGE_POSITION_PCT", 0.45
)
SPX_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT = _env_float(
    "SPX_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT", 0.10
)
SPX_BUY_TREND_MA_DAYS = _env_int("SPX_BUY_TREND_MA_DAYS", BUY_TREND_MA_DAYS)
SPX_BUY_TREND_SLOPE_LOOKBACK_DAYS = _env_int(
    "SPX_BUY_TREND_SLOPE_LOOKBACK_DAYS", BUY_TREND_SLOPE_LOOKBACK_DAYS
)
SPX_BUY_TREND_MIN_MA_SLOPE_PCT = _env_float(
    "SPX_BUY_TREND_MIN_MA_SLOPE_PCT", -0.02
)
SPX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "SPX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.30
)
SPX_BUY_NEAR_YEAR_LOW_RANGE_PCT = _env_float(
    "SPX_BUY_NEAR_YEAR_LOW_RANGE_PCT", BUY_NEAR_YEAR_LOW_RANGE_PCT
)
SPX_BUY_NEAR_YEAR_LOW_PE_RELAX = _env_float(
    "SPX_BUY_NEAR_YEAR_LOW_PE_RELAX", BUY_NEAR_YEAR_LOW_PE_RELAX
)
SPX_BUY_NEAR_YEAR_LOW_RATE_RELAX = _env_float(
    "SPX_BUY_NEAR_YEAR_LOW_RATE_RELAX", 12
)

# --- 信号强度与冷却期（默认关闭：频次由硬门槛自然决定，不再人为限频）---
BUY_COOLDOWN_ENABLED = _env_bool("BUY_COOLDOWN_ENABLED", False)
BUY_COOLDOWN_DAYS = _env_int("BUY_COOLDOWN_DAYS", 10)
BUY_COOLDOWN_DROP_OVERRIDE_ENABLED = _env_bool("BUY_COOLDOWN_DROP_OVERRIDE_ENABLED", True)
BUY_COOLDOWN_DROP_OVERRIDE_PCT = _env_float("BUY_COOLDOWN_DROP_OVERRIDE_PCT", 0.05)
BUY_COOLDOWN_AMOUNT_SCALE_ENABLED = _env_bool("BUY_COOLDOWN_AMOUNT_SCALE_ENABLED", True)
BUY_COOLDOWN_AMOUNT_MAX_MULTIPLIER = _env_float("BUY_COOLDOWN_AMOUNT_MAX_MULTIPLIER", 3.0)
BUY_COOLDOWN_AMOUNT_LOOKBACK_DAYS = _env_int("BUY_COOLDOWN_AMOUNT_LOOKBACK_DAYS", 756)
# 冷却开启时，这些指数仍可单独豁免（历史兼容；默认关闭冷却后无效）
BUY_COOLDOWN_DISABLED_CODES = frozenset(
    c.strip().upper()
    for c in os.environ.get("BUY_COOLDOWN_DISABLED_CODES", "NDX,SPX").split(",")
    if c.strip()
)
SIGNAL_STRENGTH_STRONG_MIN = _env_int("SIGNAL_STRENGTH_STRONG_MIN", 60)
SIGNAL_STRENGTH_ELIGIBLE_MIN = _env_int("SIGNAL_STRENGTH_ELIGIBLE_MIN", 40)
SIGNAL_NEAR_BUY_MARGIN_PCT = _env_float("SIGNAL_NEAR_BUY_MARGIN_PCT", 5.0)
SIGNAL_COMPARISON_ENABLED = _env_bool("SIGNAL_COMPARISON_ENABLED", True)


def buy_cooldown_enabled(index_code: str | None = None) -> bool:
    """单只指数是否应用买入冷却期。"""
    if not BUY_COOLDOWN_ENABLED:
        return False
    if index_code and index_code.upper() in BUY_COOLDOWN_DISABLED_CODES:
        return False
    return True

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


# 无日度国债数据时按年回填（2024-09 起用接口真实日度数据）
BOND_YIELD_FALLBACK_BY_YEAR = {
    2015: _env_float("BOND_YIELD_2015", 0.0335),
    2016: _env_float("BOND_YIELD_2016", 0.0305),
    2017: _env_float("BOND_YIELD_2017", 0.0358),
    2018: _env_float("BOND_YIELD_2018", 0.0322),
    2019: _env_float("BOND_YIELD_2019", 0.0318),
    2020: _env_float("BOND_YIELD_2020", 0.0291),
    2021: _env_float("BOND_YIELD_2021", 0.030),
    2022: _env_float("BOND_YIELD_2022", 0.0295),
    2023: _env_float("BOND_YIELD_2023", 0.024),
    2024: _env_float("BOND_YIELD_2024", 0.0275),
}

BOND_HISTORY_PAGE_SIZE = _env_int("BOND_HISTORY_PAGE_SIZE", 500)
BOND_HISTORY_MAX_PAGES = _env_int("BOND_HISTORY_MAX_PAGES", 50)
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT", 15)
BOND_REQUEST_TIMEOUT = _env_int("BOND_REQUEST_TIMEOUT", 10)
PUSH_REQUEST_TIMEOUT = _env_int("PUSH_REQUEST_TIMEOUT", 15)

# 数据源地址默认值见 data_sources.py；以下可由 push.env / 环境变量覆盖
BOND_YIELD_URL = _env_str("BOND_YIELD_URL", _DEFAULT_BOND_YIELD_URL)
INDEX_PERF_URL = _env_str("INDEX_PERF_URL", _DEFAULT_INDEX_PERF_URL)
SERVERCHAN_API_URL = _env_str("SERVERCHAN_API_URL", _DEFAULT_SERVERCHAN_API_URL)
CSINDEX_INDICATOR_BASE_URL = _env_str(
    "CSINDEX_INDICATOR_BASE_URL", _DEFAULT_CSINDEX_INDICATOR_BASE_URL
)
TENCENT_QUOTE_URL = _env_str("TENCENT_QUOTE_URL", _DEFAULT_TENCENT_QUOTE_URL)
FRED_CSV_BASE_URL = _env_str("FRED_CSV_BASE_URL", _DEFAULT_FRED_CSV_BASE_URL)
FRED_NASDAQ100_SERIES = _env_str(
    "FRED_NASDAQ100_SERIES", _DEFAULT_FRED_NASDAQ100_SERIES
)
SHILLER_IE_DATA_URL = _env_str("SHILLER_IE_DATA_URL", _DEFAULT_SHILLER_IE_DATA_URL)

_bond_token = os.environ.get("BOND_YIELD_TOKEN")
if _bond_token:
    BOND_YIELD_PARAMS = {**BOND_YIELD_PARAMS, "token": _bond_token}


def indicator_xls_url(index_code):
    """中证指数指标文件地址（尊重环境变量覆盖后的 BASE URL）。"""
    return f"{CSINDEX_INDICATOR_BASE_URL}/{index_code}indicator.xls"


def fred_csv_url(series_id=None):
    """FRED CSV 下载地址（尊重环境变量覆盖后的 BASE URL）。"""
    series = series_id or FRED_NASDAQ100_SERIES
    return f"{FRED_CSV_BASE_URL}?id={series}"


def load_config():
    """读取推送相关配置（环境变量优先，其次 push.env）。"""
    config = {
        "serverchan_sendkey": os.environ.get("SERVERCHAN_SENDKEY", "").strip(),
    }
    if config["serverchan_sendkey"]:
        return config

    for path in [CONFIG_FILE]:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lower()
            value = value.strip().strip('"').strip("'")
            if key == "serverchan_sendkey" and value:
                config["serverchan_sendkey"] = value
        if config["serverchan_sendkey"]:
            break
    return config


def select_indices(codes=None):
    """按代码筛选指数；未指定时返回全部。"""
    if not codes:
        return list(INDICES)

    known = {item["code"]: item for item in INDICES}
    selected = []
    for code in codes:
        if code not in known:
            available = ", ".join(known)
            raise ValueError(f"未知指数代码: {code}，可选: {available}")
        if known[code] not in selected:
            selected.append(known[code])
    return selected
