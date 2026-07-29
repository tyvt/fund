"""项目公共配置：默认值、环境变量与 push.env 覆盖。"""

import os
from pathlib import Path

from data_sources import (
    BOND_YIELD_URL as _DEFAULT_BOND_YIELD_URL,
    CSINDEX_CLOSEWEIGHT_BASE_URL as _DEFAULT_CSINDEX_CLOSEWEIGHT_BASE_URL,
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

INDICES = [
    {"code": "930955", "name": "中证红利低波100"},
    {"code": "H30269", "name": "中证红利低波动"},
]

# 红利价格指数 → 中证全收益指数（分红再投资，同源 csindex-home/perf API）
DIVIDEND_TOTAL_RETURN_INDEX = {
    "930955": "H20955",
    "H30269": "H20269",
}


def get_dividend_total_return_code(index_code):
    """红利价格指数对应的全收益指数代码（含分红再投资）。"""
    return DIVIDEND_TOTAL_RETURN_INDEX.get(index_code)

A500_INDEX = {"code": "000510", "name": "中证A500"}
A500_MARKET_DATA_START = "2024-09-03"  # 行情起点；与中证500（000905）为不同指数
HS300_INDEX = {"code": "000300", "name": "沪深300"}
ZZ500_INDEX = {"code": "000905", "name": "中证500"}
ZZ1000_INDEX = {"code": "000852", "name": "中证1000"}
KC50_INDEX = {"code": "000688", "name": "科创50"}
CYB_INDEX = {"code": "399006", "name": "创业板指"}
HSTECH_INDEX = {"code": "HSTECH", "name": "恒生科技指数"}
HSTECH_MARKET_DATA_START = "2020-07-27"
NDX_INDEX = {"code": "NDX", "name": "纳斯达克100"}
SPX_INDEX = {"code": "SPX", "name": "标普500"}

CN_BROAD_INDICES = [
    A500_INDEX,
    HS300_INDEX,
    ZZ500_INDEX,
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


_load_env_files()

# --- 回测单次买入金额（元）---
# 旧版按模块统一金额（仍兼容）；组合模式见下方 PORTFOLIO_* 与 BACKTEST_BUY_AMOUNT_BY_CODE
DIVIDEND_BUY_AMOUNT = _env_float("DIVIDEND_BUY_AMOUNT", 300)
CN_BROAD_BUY_AMOUNT = _env_float("CN_BROAD_BUY_AMOUNT", 100)
BACKTEST_OTHER_BUY_AMOUNT = _env_float("BACKTEST_OTHER_BUY_AMOUNT", 300)

# --- 组合仓位（回测总投入不变前提下，按组分配单次买入金额）---
# 核心 50%：红利 + A500 | 美股 20% | 科创50 10% | 卫星 20%：创业板 + 中证1000
# 沪深300 / 中证500 / 恒科：不纳入组合（单次买入 0）
PORTFOLIO_GROUP_WEIGHTS = {
    "core": 0.50,
    "us": 0.20,
    "kc50": 0.10,
    "satellite": 0.20,
}
PORTFOLIO_INDEX_GROUPS = {
    "930955": "core",
    "H30269": "core",
    "000510": "core",
    "NDX": "us",
    "SPX": "us",
    "000688": "kc50",
    "399006": "satellite",
    "000852": "satellite",
}
PORTFOLIO_EXCLUDED_CODES = frozenset({"000300", "000905", "HSTECH"})
# 基准总投入（元）：与收紧阈值后全指数回测一致，组合模式保持此总额
PORTFOLIO_TOTAL_BUDGET = _env_float("PORTFOLIO_TOTAL_BUDGET", 316_200)
# 组内分配：return_weighted = 按各指数历史收益率加权（回测优选）
PORTFOLIO_IN_GROUP_SPLIT = _env_str("PORTFOLIO_IN_GROUP_SPLIT", "return_weighted")

# 各指数单次买入金额（元）；由 optimize_portfolio_amounts.py 生成，可用 {代码}_BUY_AMOUNT 覆盖
# 组内倾斜（回测优选）：美股组 NDX 占 85%；卫星组创业板占 80%
PORTFOLIO_US_NDX_SHARE = _env_float("PORTFOLIO_US_NDX_SHARE", 0.85)
PORTFOLIO_SAT_CYB_SHARE = _env_float("PORTFOLIO_SAT_CYB_SHARE", 0.80)

# --- 买入金额分档（回测/实盘参考，见 buy_amount_tiers.py）---
# 按年区间位置：越低（近年内低点）投入越多；归一化后总投入接近 PORTFOLIO_TOTAL_BUDGET
BUY_AMOUNT_TIER_SCHEME = _env_str("BUY_AMOUNT_TIER_SCHEME", "range_4_mild")
BUY_AMOUNT_TIER_ENABLED = _env_bool("BUY_AMOUNT_TIER_ENABLED", True)
# 默认使用收益最大化分指数金额（非组合 50/20/10/20）；设为 false 则回退模块统一金额
BUY_AMOUNT_RETURN_MAX = _env_bool("BUY_AMOUNT_RETURN_MAX", True)

# 收益最大化基准单次买入（元）；optimize_return_max_amounts.py 2016–2025
# 美股 NDX/SPX 经 optimize_us_quota_friendly.py 调整为限购友好（放宽标准+降低单次金额）
BUY_AMOUNT_BASE_BY_CODE = {
    "NDX": 880,
    "SPX": 210,
    "399006": 118,
    "000688": 38,
    "930955": 28,
    "H30269": 28,
    "000510": 28,
    "000300": 28,
    "000905": 28,
    "000852": 28,
    "HSTECH": 28,
}

# 组合模式分指数金额（--portfolio）；与收益最大化配置独立
_BACKTEST_BUY_AMOUNT_DEFAULTS = {
    "930955": 944,
    "H30269": 902,
    "000510": 638,
    "000300": 0,
    "000905": 0,
    "000852": 49,
    "000688": 155,
    "399006": 239,
    "HSTECH": 0,
    "NDX": 256,
    "SPX": 62,
}


def _env_buy_amount_for_code(code, default):
    return _env_float_any((f"{code}_BUY_AMOUNT", f"BACKTEST_{code}_BUY_AMOUNT"), default)


def get_buy_amount_base(index_code):
    """单只指数基准单次买入金额（元）。"""
    default = BUY_AMOUNT_BASE_BY_CODE.get(index_code, 0)
    return _env_buy_amount_for_code(index_code, default)


def get_backtest_buy_amount(index_code, amounts=None):
    """读取单只指数单次买入金额（元）；0 表示不买入。"""
    if amounts is not None:
        by_code = amounts.get("by_code")
        if by_code is not None and index_code in by_code:
            return float(by_code[index_code])
        if amounts.get("unified"):
            return float(amounts["dividend"])
        if amounts.get("portfolio") and index_code in PORTFOLIO_EXCLUDED_CODES:
            return 0.0
        if index_code in ("930955", "H30269"):
            return float(amounts.get("dividend", DIVIDEND_BUY_AMOUNT))
        if index_code in {i["code"] for i in CN_BROAD_INDICES}:
            return float(amounts.get("cn_broad", CN_BROAD_BUY_AMOUNT))
        return float(amounts.get("other", BACKTEST_OTHER_BUY_AMOUNT))
    return get_buy_amount_base(index_code)


def _build_portfolio_by_code(buy_counts, index_returns, total_budget=None):
    """按组合权重 + 组内收益率加权，计算各指数单次买入金额。"""
    budget = total_budget if total_budget is not None else PORTFOLIO_TOTAL_BUDGET
    group_codes = {}
    for code, group in PORTFOLIO_INDEX_GROUPS.items():
        group_codes.setdefault(group, []).append(code)
    by_code = {code: 0.0 for code in buy_counts}
    for code in PORTFOLIO_EXCLUDED_CODES:
        by_code[code] = 0.0
    for group, weight in PORTFOLIO_GROUP_WEIGHTS.items():
        codes = [c for c in group_codes.get(group, []) if buy_counts.get(c, 0) > 0]
        if not codes:
            continue
        group_budget = budget * weight
        if PORTFOLIO_IN_GROUP_SPLIT == "equal":
            total_buys = sum(buy_counts[c] for c in codes)
            per_buy = group_budget / total_buys if total_buys else 0
            for c in codes:
                by_code[c] = per_buy
        else:
            # return_weighted：amount_i = B * r_i / sum(n_j * r_j)
            scores = []
            for c in codes:
                r = max(index_returns.get(c, 0), 0.01)
                scores.append((c, buy_counts[c] * r))
            denom = sum(s for _, s in scores) or 1
            for c, _ in scores:
                r = max(index_returns.get(c, 0), 0.01)
                by_code[c] = group_budget * r / denom
    return by_code


def resolve_backtest_amounts(
    unified_amount=None,
    portfolio_mode=False,
    return_max_mode=None,
    tier_enabled=None,
):
    """回测买入金额。默认收益最大化分指数 + 分档；portfolio_mode 为组合权重模式。"""
    if tier_enabled is None:
        tier_enabled = BUY_AMOUNT_TIER_ENABLED
    if unified_amount is not None and unified_amount > 0:
        return {
            "dividend": unified_amount,
            "cn_broad": unified_amount,
            "other": unified_amount,
            "unified": True,
            "portfolio": False,
            "return_max": False,
            "by_code": None,
            "tier_scheme": BUY_AMOUNT_TIER_SCHEME if tier_enabled else None,
            "tier_normalize": tier_enabled,
        }
    if portfolio_mode:
        by_code = {
            code: _env_buy_amount_for_code(code, amt)
            for code, amt in _BACKTEST_BUY_AMOUNT_DEFAULTS.items()
        }
        for code in PORTFOLIO_EXCLUDED_CODES:
            by_code[code] = 0.0
        return {
            "dividend": DIVIDEND_BUY_AMOUNT,
            "cn_broad": CN_BROAD_BUY_AMOUNT,
            "other": BACKTEST_OTHER_BUY_AMOUNT,
            "unified": False,
            "portfolio": True,
            "return_max": False,
            "by_code": by_code,
            "total_budget": PORTFOLIO_TOTAL_BUDGET,
            "group_weights": dict(PORTFOLIO_GROUP_WEIGHTS),
            "tier_scheme": BUY_AMOUNT_TIER_SCHEME if tier_enabled else None,
            "tier_normalize": tier_enabled,
        }
    if return_max_mode is None:
        return_max_mode = BUY_AMOUNT_RETURN_MAX
    if return_max_mode:
        from buy_amount_config import ALL_BUY_INDEX_CODES

        by_code = {code: get_buy_amount_base(code) for code in ALL_BUY_INDEX_CODES}
        return {
            "dividend": DIVIDEND_BUY_AMOUNT,
            "cn_broad": CN_BROAD_BUY_AMOUNT,
            "other": BACKTEST_OTHER_BUY_AMOUNT,
            "unified": False,
            "portfolio": False,
            "return_max": True,
            "by_code": by_code,
            "total_budget": PORTFOLIO_TOTAL_BUDGET,
            "tier_scheme": BUY_AMOUNT_TIER_SCHEME if tier_enabled else None,
            "tier_normalize": tier_enabled,
        }
    return {
        "dividend": DIVIDEND_BUY_AMOUNT,
        "cn_broad": CN_BROAD_BUY_AMOUNT,
        "other": BACKTEST_OTHER_BUY_AMOUNT,
        "unified": False,
        "portfolio": False,
        "return_max": False,
        "by_code": None,
        "tier_scheme": BUY_AMOUNT_TIER_SCHEME if tier_enabled else None,
        "tier_normalize": tier_enabled,
    }


def format_backtest_amount_note(amounts):
    """Markdown/控制台用的买入金额说明。"""
    if amounts is None:
        return "仅统计次数"
    tier = amounts.get("tier_scheme")
    tier_suffix = f" + 分档 **{tier}**" if tier else ""
    if amounts.get("unified"):
        return f"每次买入 **{amounts['dividend']:.0f}** 元{tier_suffix}"
    if amounts.get("return_max") and amounts.get("by_code"):
        active = {c: a for c, a in amounts["by_code"].items() if a and a > 0}
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        budget = amounts.get("total_budget")
        head = f"收益最大化分指数（总预算 **{budget:,.0f}** 元）" if budget else "收益最大化分指数"
        return head + tier_suffix + "：" + "；".join(parts)
    if amounts.get("portfolio") and amounts.get("by_code"):
        active = {
            c: a for c, a in amounts["by_code"].items() if a and a > 0
        }
        parts = [f"{c} **{a:.0f}**" for c, a in sorted(active.items())]
        budget = amounts.get("total_budget")
        head = f"组合模式（总预算 **{budget:,.0f}** 元）" if budget else "组合模式"
        return head + tier_suffix + "：" + "；".join(parts)
    parts = [
        f"红利每次 **{amounts['dividend']:.0f}** 元",
        f"宽基每次 **{amounts['cn_broad']:.0f}** 元",
    ]
    if amounts["other"] != amounts["cn_broad"]:
        parts.append(f"其他每次 **{amounts['other']:.0f}** 元")
    return "；".join(parts)

# =============================================================================
# 各指数买入 / 卖出信号阈值（可通过 push.env 覆盖）
# =============================================================================

# --- 红利指数（930955 / H30269，按指数拆分阈值）---
# 买入：股息率-国债利差 > 阈值，且利差分位高、PE 分位低、距 N 日低点涨幅不过高（须同时满足）。
# 红利指数仅配置买入阈值，不设卖点。
# 两只指数价格走势接近，但 PE 中枢不同（低波100 约 8.5，低波动约 7.5），
# 利差分位历史分布也不同，故使用分指数默认阈值；可用 DIVIDEND_{代码}_* 覆盖。
# 默认阈值经 2015 至今回测：利差 3.0%、H30269 利差分位 42%/PE 72、930955 PE 68，
# 较旧版买入次 +62、两只指数收益率均不降（H30269 +5.8pp）。
DIVIDEND_BUY_SPREAD_MIN = _env_float_any(
    ("DIVIDEND_BUY_SPREAD_MIN", "BUY_CONDITION_SPREAD"), 0.030
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
DIVIDEND_BUY_MAX_YEAR_RANGE_PCT = _env_float_any(
    ("DIVIDEND_BUY_MAX_YEAR_RANGE_PCT",), 0.60
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

_DIVIDEND_CFG_SUFFIX = {
    "buy_spread_min": "BUY_SPREAD_MIN",
    "buy_spread_percentile_min": "BUY_SPREAD_PERCENTILE_MIN",
    "buy_pe_percentile_max": "BUY_PE_PERCENTILE_MAX",
    "buy_max_above_low_pct": "BUY_MAX_ABOVE_LOW_PCT",
    "buy_low_lookback_days": "BUY_LOW_LOOKBACK_DAYS",
    "buy_high_lookback_days": "BUY_HIGH_LOOKBACK_DAYS",
    "buy_min_drawdown_from_high_pct": "BUY_MIN_DRAWDOWN_FROM_HIGH_PCT",
    "buy_max_year_range_pct": "BUY_MAX_YEAR_RANGE_PCT",
    "buy_near_year_low_range_pct": "BUY_NEAR_YEAR_LOW_RANGE_PCT",
    "buy_near_year_low_spread_relax": "BUY_NEAR_YEAR_LOW_SPREAD_RELAX",
    "buy_near_year_low_pe_relax": "BUY_NEAR_YEAR_LOW_PE_RELAX",
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
    "buy_max_year_range_pct": DIVIDEND_BUY_MAX_YEAR_RANGE_PCT,
    "buy_near_year_low_range_pct": DIVIDEND_BUY_NEAR_YEAR_LOW_RANGE_PCT,
    "buy_near_year_low_spread_relax": DIVIDEND_BUY_NEAR_YEAR_LOW_SPREAD_RELAX,
    "buy_near_year_low_pe_relax": DIVIDEND_BUY_NEAR_YEAR_LOW_PE_RELAX,
    "buy_near_year_low_above_low_relax": BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
    "buy_mid_range_position_pct": BUY_MID_RANGE_POSITION_PCT,
    "buy_mid_range_max_above_low_pct": BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
}

_DIVIDEND_PER_INDEX_DEFAULTS = {
    "930955": {
        "buy_spread_percentile_min": 48,
        "buy_pe_percentile_max": 68,
        "buy_max_above_low_pct": 0.04,
        "buy_max_year_range_pct": 0.55,
    },
    "H30269": {
        "buy_spread_percentile_min": 42,
        "buy_pe_percentile_max": 72,
        "buy_max_year_range_pct": 0.55,
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

# --- A 股宽基（五只指数各自完整阈值，不回退 CN_BROAD_* 全局默认）---
# 买入：股债利差分位 + PE/PB 分位 + 价格位置（多数指标 favorable）；卖出见 cn_broad_signal。
# 覆盖方式：CN_BROAD_{代码}_*；中证 A500 另兼容 A500_*。
_CN_BROAD_CFG_SUFFIX = {
    "buy_spread_percentile_min": "BUY_SPREAD_PERCENTILE_MIN",
    "buy_pe_percentile_max": "BUY_PE_PERCENTILE_MAX",
    "buy_pb_percentile_max": "BUY_PB_PERCENTILE_MAX",
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
    "buy_near_year_low_pe_relax": "BUY_NEAR_YEAR_LOW_PE_RELAX",
    "buy_near_year_low_above_low_relax": "BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX",
    "buy_near_year_low_drawdown_waive_pct": "BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT",
    "buy_mid_range_position_pct": "BUY_MID_RANGE_POSITION_PCT",
    "buy_mid_range_max_above_low_pct": "BUY_MID_RANGE_MAX_ABOVE_LOW_PCT",
    "buy_range_lookback_days": "BUY_RANGE_LOOKBACK_DAYS",
    "buy_trend_ma_days": "BUY_TREND_MA_DAYS",
    "buy_trend_slope_lookback_days": "BUY_TREND_SLOPE_LOOKBACK_DAYS",
    "buy_trend_min_ma_slope_pct": "BUY_TREND_MIN_MA_SLOPE_PCT",
    "buy_trend_downtrend_max_range_pct": "BUY_TREND_DOWNTREND_MAX_RANGE_PCT",
    "sell_spread_percentile_max": "SELL_SPREAD_PERCENTILE_MAX",
    "sell_pe_percentile_min": "SELL_PE_PERCENTILE_MIN",
    "sell_pb_percentile_min": "SELL_PB_PERCENTILE_MIN",
    "sell_max_above_low_pct": "SELL_MAX_ABOVE_LOW_PCT",
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
}


def _cn_broad_index_defaults(**overrides):
    """构造单只宽基指数的完整默认阈值（五只指数各自独立，仅用于初始化）。"""
    cfg = {
        "buy_spread_percentile_min": 55,
        "buy_pe_percentile_max": 68,
        "buy_pb_percentile_max": 66,
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
        "buy_near_year_low_pe_relax": 12.0,
        "buy_near_year_low_above_low_relax": 0.04,
        "buy_near_year_low_drawdown_waive_pct": 0.12,
        "buy_mid_range_position_pct": 0.45,
        "buy_mid_range_max_above_low_pct": 0.06,
        "buy_range_lookback_days": 252,
        "buy_trend_ma_days": 200,
        "buy_trend_slope_lookback_days": 60,
        "buy_trend_min_ma_slope_pct": -0.02,
        "buy_trend_downtrend_max_range_pct": 0.10,
        "sell_spread_percentile_max": 25.0,
        "sell_pe_percentile_min": 88.0,
        "sell_pb_percentile_min": 99.0,
        "sell_max_above_low_pct": 0.20,
    }
    cfg.update(overrides)
    return cfg


_CN_BROAD_PER_INDEX_DEFAULTS = {
    # 中证 A500：2024-09 发布，样本短，阈值略宽
    "000510": _cn_broad_index_defaults(
        buy_spread_percentile_min=50,
        buy_pe_percentile_max=72,
        buy_pb_percentile_max=68,
        buy_max_above_low_pct=0.10,
        buy_low_lookback_days=90,
        buy_min_drawdown_from_high_pct=None,
        buy_max_year_range_pct=0.58,
        buy_mid_range_max_above_low_pct=0.08,
        sell_spread_percentile_max=22,
        sell_pe_percentile_min=92,
        sell_max_above_low_pct=0.22,
    ),
    # 沪深300：二次收紧（夏普持续偏低）
    "000300": _cn_broad_index_defaults(
        buy_spread_percentile_min=65,
        buy_pe_percentile_max=54,
        buy_pb_percentile_max=58,
        buy_max_above_low_pct=0.05,
        buy_min_drawdown_from_high_pct=0.16,
        buy_max_year_range_pct=0.36,
        buy_mid_range_max_above_low_pct=0.04,
        buy_trend_min_ma_slope_pct=-0.010,
        buy_trend_downtrend_max_range_pct=0.05,
        sell_spread_percentile_max=25,
        sell_pe_percentile_min=85,
        sell_max_above_low_pct=0.18,
    ),
    # 中证500：二次收紧
    "000905": _cn_broad_index_defaults(
        buy_spread_percentile_min=68,
        buy_pe_percentile_max=54,
        buy_pb_percentile_max=58,
        buy_max_above_low_pct=0.05,
        buy_min_drawdown_from_high_pct=0.16,
        buy_max_year_range_pct=0.36,
        buy_mid_range_max_above_low_pct=0.04,
        sell_spread_percentile_max=22,
        sell_pe_percentile_min=92,
        sell_max_above_low_pct=0.24,
    ),
    # 中证1000：二次收紧（最严）
    "000852": _cn_broad_index_defaults(
        buy_spread_percentile_min=70,
        buy_pe_percentile_max=52,
        buy_pb_percentile_max=56,
        buy_max_above_low_pct=0.05,
        buy_min_drawdown_from_high_pct=0.16,
        buy_max_year_range_pct=0.34,
        buy_mid_range_max_above_low_pct=0.04,
        sell_spread_percentile_max=25,
        sell_pe_percentile_min=88,
        sell_max_above_low_pct=0.22,
    ),
    # 科创50：夏普偏低但绝对收益高，轻度收紧
    "000688": _cn_broad_index_defaults(
        buy_spread_percentile_min=64,
        buy_pe_percentile_max=52,
        buy_pb_percentile_max=58,
        buy_max_above_low_pct=0.07,
        buy_min_drawdown_from_high_pct=0.14,
        buy_max_year_range_pct=0.38,
        buy_mid_range_max_above_low_pct=0.05,
        buy_trend_downtrend_max_range_pct=0.06,
        sell_spread_percentile_max=22,
        sell_pe_percentile_min=92,
        sell_max_above_low_pct=0.25,
    ),
}


def get_cn_broad_signal_config(index_code):
    """读取单只 A 股宽基指数的买入/卖出阈值（仅分指数默认 + 分指数环境变量）。"""
    per_index = _CN_BROAD_PER_INDEX_DEFAULTS.get(index_code)
    if per_index is None:
        raise ValueError(f"未知宽基指数代码: {index_code}")
    cfg = {}
    for key, suffix in _CN_BROAD_CFG_SUFFIX.items():
        default = per_index[key]
        env_names = [f"CN_BROAD_{index_code}_{suffix}"]
        if index_code == "000510":
            env_names.append(f"A500_{suffix}")
        if key == "buy_require_spread":
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
    return cfg


# 兼容旧变量名（A500 / 指向 000510 默认）
_a500_defaults = _CN_BROAD_PER_INDEX_DEFAULTS["000510"]
A500_BUY_SPREAD_PERCENTILE_MIN = _env_float(
    "A500_BUY_SPREAD_PERCENTILE_MIN", _a500_defaults["buy_spread_percentile_min"]
)
A500_BUY_PE_PERCENTILE_MAX = _env_float(
    "A500_BUY_PE_PERCENTILE_MAX", _a500_defaults["buy_pe_percentile_max"]
)
A500_BUY_PB_PERCENTILE_MAX = _env_float(
    "A500_BUY_PB_PERCENTILE_MAX", _a500_defaults["buy_pb_percentile_max"]
)
A500_BUY_REQUIRE_SPREAD = _env_bool(
    "A500_BUY_REQUIRE_SPREAD", _a500_defaults["buy_require_spread"]
)
A500_BUY_MIN_APPLICABLE_CRITERIA = _env_int(
    "A500_BUY_MIN_APPLICABLE_CRITERIA", _a500_defaults["buy_min_applicable_criteria"]
)
A500_BUY_MIN_PASS_SCORE_FLOOR = _env_int(
    "A500_BUY_MIN_PASS_SCORE_FLOOR", _a500_defaults["buy_min_pass_score_floor"]
)
A500_PERCENTILE_WINDOW = _env_int(
    "A500_PERCENTILE_WINDOW", _a500_defaults["percentile_window"]
)
A500_PERCENTILE_MIN_DAYS = _env_int(
    "A500_PERCENTILE_MIN_DAYS", _a500_defaults["percentile_min_days"]
)
A500_BUY_MAX_ABOVE_LOW_PCT = _env_float(
    "A500_BUY_MAX_ABOVE_LOW_PCT", _a500_defaults["buy_max_above_low_pct"]
)
A500_BUY_LOW_LOOKBACK_DAYS = _env_int(
    "A500_BUY_LOW_LOOKBACK_DAYS", _a500_defaults["buy_low_lookback_days"]
)

# --- 创业板指（399006）---
# 买入：加权 PE/PB 分位偏低 + PEG(近5年增速) ≤ 阈值（三项须同时满足）
CYB_EXPECTED_GROWTH = _env_float("CYB_EXPECTED_GROWTH", 0.3906)
CYB_HISTORICAL_GROWTH = _env_float("CYB_HISTORICAL_GROWTH", 0.1663)
CYB_ROE_AVG = _env_float("CYB_ROE_AVG", 0.1229)
# 创业板指：2016-2025 夏普偏低(0.41)且回撤大，收紧买入
CYB_BUY_PE_PERCENTILE_MAX = _env_float("CYB_BUY_PE_PERCENTILE_MAX", 46)
CYB_BUY_PB_PERCENTILE_MAX = _env_float("CYB_BUY_PB_PERCENTILE_MAX", 38)
CYB_BUY_PEG_EXPECTED_MAX = _env_float("CYB_BUY_PEG_EXPECTED_MAX", 1.1)
CYB_BUY_PEG_HIST_MAX = _env_float("CYB_BUY_PEG_HIST_MAX", 2.2)
CYB_SELL_PE_PERCENTILE_MIN = _env_float("CYB_SELL_PE_PERCENTILE_MIN", 78)
CYB_SELL_PB_PERCENTILE_MIN = _env_float("CYB_SELL_PB_PERCENTILE_MIN", 78)
CYB_SELL_PEG_HIST_MIN = _env_float("CYB_SELL_PEG_HIST_MIN", 3.0)
CYB_SELL_COMBO_PE_PERCENTILE_MIN = _env_float("CYB_SELL_COMBO_PE_PERCENTILE_MIN", 60)
CYB_SELL_COMBO_PB_PERCENTILE_MIN = _env_float("CYB_SELL_COMBO_PB_PERCENTILE_MIN", 60)
CYB_PERCENTILE_WINDOW = _env_int("CYB_PERCENTILE_WINDOW", 2520)
CYB_DIV_PERCENTILE_WINDOW = _env_int("CYB_DIV_PERCENTILE_WINDOW", 1260)
CYB_PERCENTILE_MIN_DAYS = _env_int("CYB_PERCENTILE_MIN_DAYS", 120)
CYB_BUY_MAX_ABOVE_LOW_PCT = _env_float("CYB_BUY_MAX_ABOVE_LOW_PCT", 0.06)
CYB_BUY_LOW_LOOKBACK_DAYS = _env_int("CYB_BUY_LOW_LOOKBACK_DAYS", 90)
CYB_BUY_HIGH_LOOKBACK_DAYS = _env_int("CYB_BUY_HIGH_LOOKBACK_DAYS", 252)
CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float(
    "CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT", 0.18
)
CYB_BUY_MAX_YEAR_RANGE_PCT = _env_float("CYB_BUY_MAX_YEAR_RANGE_PCT", 0.42)
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
CYB_BUY_NEAR_YEAR_LOW_PE_RELAX = _env_float(
    "CYB_BUY_NEAR_YEAR_LOW_PE_RELAX", BUY_NEAR_YEAR_LOW_PE_RELAX
)

# --- 恒生科技指数（HSTECH，2020-07 发布，历史约 5 年）---
# 买入：PE 分位偏低 + PEG(近5年增速) ≤ 阈值 + 股息率分位偏高 + 价格位置（须同时满足；乐咕暂无 PB/PS 历史）
HSTECH_HISTORICAL_GROWTH = _env_float("HSTECH_HISTORICAL_GROWTH", 0.15)
HSTECH_BUY_PE_PERCENTILE_MAX = _env_float("HSTECH_BUY_PE_PERCENTILE_MAX", 38)
HSTECH_BUY_PEG_HIST_MAX = _env_float("HSTECH_BUY_PEG_HIST_MAX", 1.6)
HSTECH_BUY_DIV_PERCENTILE_MIN = _env_float("HSTECH_BUY_DIV_PERCENTILE_MIN", 50)
HSTECH_SELL_PE_PERCENTILE_MIN = _env_float("HSTECH_SELL_PE_PERCENTILE_MIN", 78)
HSTECH_SELL_PEG_HIST_MIN = _env_float("HSTECH_SELL_PEG_HIST_MIN", 3.0)
# 动态卖出：PE 分位偏高时，须 PEG 过高或距近1年低点涨幅过大（避免估值钝化时过早/过晚卖）
HSTECH_SELL_ABOVE_LOW_MIN = _env_float("HSTECH_SELL_ABOVE_LOW_MIN", 0.40)
HSTECH_SELL_COST_LOOKBACK_DAYS = _env_int("HSTECH_SELL_COST_LOOKBACK_DAYS", 252)
HSTECH_PERCENTILE_WINDOW = _env_int("HSTECH_PERCENTILE_WINDOW", 1260)
HSTECH_DIV_PERCENTILE_WINDOW = _env_int("HSTECH_DIV_PERCENTILE_WINDOW", 756)
HSTECH_PERCENTILE_MIN_DAYS = _env_int("HSTECH_PERCENTILE_MIN_DAYS", 60)
HSTECH_BUY_MAX_ABOVE_LOW_PCT = _env_float("HSTECH_BUY_MAX_ABOVE_LOW_PCT", 0.08)
HSTECH_BUY_LOW_LOOKBACK_DAYS = _env_int("HSTECH_BUY_LOW_LOOKBACK_DAYS", 252)
HSTECH_BUY_HIGH_LOOKBACK_DAYS = _env_int("HSTECH_BUY_HIGH_LOOKBACK_DAYS", 252)
HSTECH_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float(
    "HSTECH_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT", 0.28
)
HSTECH_BUY_MAX_YEAR_RANGE_PCT = _env_float("HSTECH_BUY_MAX_YEAR_RANGE_PCT", 0.42)
HSTECH_BUY_MID_RANGE_POSITION_PCT = _env_float(
    "HSTECH_BUY_MID_RANGE_POSITION_PCT", 0.45
)
HSTECH_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT = _env_float(
    "HSTECH_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT", 0.06
)
HSTECH_BUY_TREND_MA_DAYS = _env_int("HSTECH_BUY_TREND_MA_DAYS", BUY_TREND_MA_DAYS)
HSTECH_BUY_TREND_SLOPE_LOOKBACK_DAYS = _env_int(
    "HSTECH_BUY_TREND_SLOPE_LOOKBACK_DAYS", BUY_TREND_SLOPE_LOOKBACK_DAYS
)
HSTECH_BUY_TREND_MIN_MA_SLOPE_PCT = _env_float(
    "HSTECH_BUY_TREND_MIN_MA_SLOPE_PCT", -0.005
)
HSTECH_BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "HSTECH_BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.04
)
HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT = _env_float(
    "HSTECH_BUY_NEAR_YEAR_LOW_RANGE_PCT", BUY_NEAR_YEAR_LOW_RANGE_PCT
)
HSTECH_BUY_NEAR_YEAR_LOW_PE_RELAX = _env_float(
    "HSTECH_BUY_NEAR_YEAR_LOW_PE_RELAX", 0
)
HSTECH_BUY_NEAR_YEAR_LOW_PEG_RELAX = _env_float(
    "HSTECH_BUY_NEAR_YEAR_LOW_PEG_RELAX", 0
)
HSTECH_BUY_NEAR_YEAR_LOW_DIV_RELAX = _env_float(
    "HSTECH_BUY_NEAR_YEAR_LOW_DIV_RELAX", 0
)

# --- 纳斯达克 100（NDX）---
# 买入：Forward PE 分位偏低 + PEG(Forward) ≤ 阈值 + 10Y 利率分位不高（三项须同时满足）
NDX_FORWARD_PE_URL = _env_str(
    "NDX_FORWARD_PE_URL",
    "https://historyofmarket.com/api/ndx/forward-pe.json",
)
NDX_DIVIDEND_PROXY_SYMBOL = _env_str("NDX_DIVIDEND_PROXY_SYMBOL", "QQQ")
NDX_EXPECTED_GROWTH = _env_float("NDX_EXPECTED_GROWTH", 0.0) or None
NDX_FALLBACK_EXPECTED_GROWTH = _env_float("NDX_FALLBACK_EXPECTED_GROWTH", 0.19)
NDX_HIGH_GROWTH_THRESHOLD = _env_float("NDX_HIGH_GROWTH_THRESHOLD", 0.20)
NDX_HIGH_GROWTH_PEG_BONUS = _env_float("NDX_HIGH_GROWTH_PEG_BONUS", 0.2)
NDX_BUY_TRAILING_PE_PERCENTILE_MAX = _env_float(
    "NDX_BUY_TRAILING_PE_PERCENTILE_MAX", 87
)
NDX_BUY_FORWARD_PE_PERCENTILE_MAX = _env_float(
    "NDX_BUY_FORWARD_PE_PERCENTILE_MAX", 85
)
NDX_BUY_PEG_FORWARD_MAX = _env_float("NDX_BUY_PEG_FORWARD_MAX", 1.72)
NDX_BUY_PEG_HIST_MAX = _env_float("NDX_BUY_PEG_HIST_MAX", 1.5)
NDX_BUY_RATE_PERCENTILE_MAX = _env_float("NDX_BUY_RATE_PERCENTILE_MAX", 99)
NDX_HISTORY_YEARS = _env_int("NDX_HISTORY_YEARS", 10)
NDX_PERCENTILE_WINDOW = _env_int("NDX_PERCENTILE_WINDOW", 120)
NDX_PERCENTILE_MIN_DAYS = _env_int("NDX_PERCENTILE_MIN_DAYS", 24)
NDX_DAILY_PERCENTILE_WINDOW = _env_int("NDX_DAILY_PERCENTILE_WINDOW", 2520)
NDX_DAILY_PERCENTILE_MIN_DAYS = _env_int("NDX_DAILY_PERCENTILE_MIN_DAYS", 252)
NDX_FRED_NETWORK_TIMEOUT = _env_int("NDX_FRED_NETWORK_TIMEOUT", 10)
NDX_BUY_MAX_ABOVE_LOW_PCT = _env_float("NDX_BUY_MAX_ABOVE_LOW_PCT", 0.19)
NDX_BUY_LOW_LOOKBACK_DAYS = _env_int("NDX_BUY_LOW_LOOKBACK_DAYS", 90)
NDX_BUY_HIGH_LOOKBACK_DAYS = _env_int("NDX_BUY_HIGH_LOOKBACK_DAYS", 252)
NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float(
    "NDX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT", 0.07
)
NDX_BUY_MAX_YEAR_RANGE_PCT = _env_float("NDX_BUY_MAX_YEAR_RANGE_PCT", 0.58)
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
    "NDX_BUY_TREND_MIN_MA_SLOPE_PCT", BUY_TREND_MIN_MA_SLOPE_PCT
)
NDX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "NDX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.12
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
NDX_BUY_NEAR_YEAR_LOW_PEG_RELAX = _env_float("NDX_BUY_NEAR_YEAR_LOW_PEG_RELAX", 0.5)

# --- 标普 500（SPX，逻辑参考纳斯达克 100）---
SPX_FORWARD_PE_URL = _env_str(
    "SPX_FORWARD_PE_URL",
    "https://historyofmarket.com/api/sp500/forward-pe.json",
)
SPX_DIVIDEND_PROXY_SYMBOL = _env_str("SPX_DIVIDEND_PROXY_SYMBOL", "SPY")
SPX_EXPECTED_GROWTH = _env_float("SPX_EXPECTED_GROWTH", 0.0) or None
SPX_FALLBACK_EXPECTED_GROWTH = _env_float("SPX_FALLBACK_EXPECTED_GROWTH", 0.10)
SPX_HIGH_GROWTH_THRESHOLD = _env_float("SPX_HIGH_GROWTH_THRESHOLD", 0.15)
SPX_HIGH_GROWTH_PEG_BONUS = _env_float("SPX_HIGH_GROWTH_PEG_BONUS", 0.15)
SPX_BUY_TRAILING_PE_PERCENTILE_MAX = _env_float(
    "SPX_BUY_TRAILING_PE_PERCENTILE_MAX", 87
)
SPX_BUY_FORWARD_PE_PERCENTILE_MAX = _env_float(
    "SPX_BUY_FORWARD_PE_PERCENTILE_MAX", 87
)
SPX_BUY_PEG_FORWARD_MAX = _env_float("SPX_BUY_PEG_FORWARD_MAX", 1.62)
SPX_BUY_PEG_HIST_MAX = _env_float("SPX_BUY_PEG_HIST_MAX", 1.45)
SPX_BUY_RATE_PERCENTILE_MAX = _env_float("SPX_BUY_RATE_PERCENTILE_MAX", 99)
SPX_HISTORY_YEARS = _env_int("SPX_HISTORY_YEARS", 10)
SPX_PERCENTILE_WINDOW = _env_int("SPX_PERCENTILE_WINDOW", 120)
SPX_PERCENTILE_MIN_DAYS = _env_int("SPX_PERCENTILE_MIN_DAYS", 24)
SPX_DAILY_PERCENTILE_WINDOW = _env_int("SPX_DAILY_PERCENTILE_WINDOW", 2520)
SPX_DAILY_PERCENTILE_MIN_DAYS = _env_int("SPX_DAILY_PERCENTILE_MIN_DAYS", 252)
SPX_FRED_NETWORK_TIMEOUT = _env_int("SPX_FRED_NETWORK_TIMEOUT", 10)
SPX_BUY_MAX_ABOVE_LOW_PCT = _env_float("SPX_BUY_MAX_ABOVE_LOW_PCT", 0.17)
SPX_BUY_LOW_LOOKBACK_DAYS = _env_int("SPX_BUY_LOW_LOOKBACK_DAYS", 90)
SPX_BUY_HIGH_LOOKBACK_DAYS = _env_int("SPX_BUY_HIGH_LOOKBACK_DAYS", 252)
SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = _env_float(
    "SPX_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT", 0.05
)
SPX_BUY_MAX_YEAR_RANGE_PCT = _env_float("SPX_BUY_MAX_YEAR_RANGE_PCT", 0.60)
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
    "SPX_BUY_TREND_MIN_MA_SLOPE_PCT", BUY_TREND_MIN_MA_SLOPE_PCT
)
SPX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT = _env_float(
    "SPX_BUY_TREND_DOWNTREND_MAX_RANGE_PCT", 0.12
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
SPX_BUY_NEAR_YEAR_LOW_PEG_RELAX = _env_float("SPX_BUY_NEAR_YEAR_LOW_PEG_RELAX", 0.5)

# --- 回测风险指标 ---
BACKTEST_RISK_FREE_RATE = _env_float("BACKTEST_RISK_FREE_RATE", 0.024)
BACKTEST_TRADING_DAYS_PER_YEAR = _env_int("BACKTEST_TRADING_DAYS_PER_YEAR", 252)

# --- 卖出开关（回测 2016-2025：仅科创50 卖出对组合收益有正贡献）---
CN_BROAD_SELL_ENABLED_CODES = frozenset({"000688"})
CYB_SELL_ENABLED = _env_bool("CYB_SELL_ENABLED", False)
HSTECH_SELL_ENABLED = _env_bool("HSTECH_SELL_ENABLED", False)


def cn_broad_sell_enabled(index_code):
    """单只 A 股宽基是否启用卖出逻辑。"""
    return index_code in CN_BROAD_SELL_ENABLED_CODES


# --- 其他参考阈值（当前代码未用于主信号逻辑，保留可配置）---
STRONG_BUY_SPREAD = _env_float("STRONG_BUY_SPREAD", 0.035)
NORMAL_BUY_SPREAD = _env_float("NORMAL_BUY_SPREAD", 0.02)
SELL_SPREAD = _env_float("SELL_SPREAD", 0.01)
STRONG_SELL_SPREAD = _env_float("STRONG_SELL_SPREAD", 0.005)
PE_LOW_PERCENTILE = _env_float("PE_LOW_PERCENTILE", 30)
PE_HIGH_PERCENTILE = _env_float("PE_HIGH_PERCENTILE", 70)
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
PB_GOOD_THRESHOLD = _env_float("PB_GOOD_THRESHOLD", 1.0)
PB_MID_THRESHOLD = _env_float("PB_MID_THRESHOLD", 1.3)
PAYOUT_RATIO_LOW = _env_float("PAYOUT_RATIO_LOW", 0.33)
PAYOUT_RATIO_HIGH = _env_float("PAYOUT_RATIO_HIGH", 0.45)
LOW_RATE_BOND_PERCENTILE = _env_float("LOW_RATE_BOND_PERCENTILE", 30)
CROWDING_LOW_PERCENTILE = _env_float("CROWDING_LOW_PERCENTILE", 30)
CROWDING_HIGH_PERCENTILE = _env_float("CROWDING_HIGH_PERCENTILE", 70)
MAX_DRAWDOWN_GOOD = _env_float("MAX_DRAWDOWN_GOOD", -0.25)
MAX_DRAWDOWN_MID = _env_float("MAX_DRAWDOWN_MID", -0.40)
VOLATILITY_WINDOW = _env_int("VOLATILITY_WINDOW", 252)

BOND_HISTORY_PAGE_SIZE = _env_int("BOND_HISTORY_PAGE_SIZE", 500)
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
CSINDEX_CLOSEWEIGHT_BASE_URL = _env_str(
    "CSINDEX_CLOSEWEIGHT_BASE_URL", _DEFAULT_CSINDEX_CLOSEWEIGHT_BASE_URL
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


def closeweight_xls_url(index_code):
    """中证指数成分股权重文件地址（尊重环境变量覆盖后的 BASE URL）。"""
    return f"{CSINDEX_CLOSEWEIGHT_BASE_URL}/{index_code}closeweight.xls"


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


def format_spread_percent(spread):
    return f"{spread * 100:.1f}%"


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
