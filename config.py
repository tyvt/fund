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

INDICES = [
    {"code": "930955", "name": "中证红利低波100"},
    {"code": "H30269", "name": "中证红利低波动"},
]

A500_INDEX = {"code": "000510", "name": "中证A500"}
A500_MARKET_DATA_START = "2024-09-03"  # 行情起点；与中证500（000905）为不同指数
HS300_INDEX = {"code": "000300", "name": "沪深300"}
ZZ500_INDEX = {"code": "000905", "name": "中证500"}
ZZ1000_INDEX = {"code": "000852", "name": "中证1000"}
KC50_INDEX = {"code": "000688", "name": "科创50"}
CYB_INDEX = {"code": "399006", "name": "创业板指"}
NDX_INDEX = {"code": "NDX", "name": "纳斯达克100"}
SPX_INDEX = {"code": "SPX", "name": "标普500"}

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

# =============================================================================
# 各指数买入 / 卖出信号阈值（可通过 push.env 覆盖）
# =============================================================================

# --- 红利指数（930955 / H30269，按指数拆分阈值）---
# 买入：股息率-国债利差 > 阈值，且利差分位高、PE 分位低、距 N 日低点涨幅不过高（须同时满足）。
# 红利指数仅配置买入阈值，不设卖点。
# 两只指数价格走势接近，但 PE 中枢不同（低波100 约 8.5，低波动约 7.5），
# 利差分位历史分布也不同，故使用分指数默认阈值；可用 DIVIDEND_{代码}_* 覆盖。
DIVIDEND_BUY_SPREAD_MIN = _env_float_any(
    ("DIVIDEND_BUY_SPREAD_MIN", "BUY_CONDITION_SPREAD"), 0.032
)
DIVIDEND_BUY_SPREAD_PERCENTILE_MIN = _env_float_any(
    ("DIVIDEND_BUY_SPREAD_PERCENTILE_MIN", "BUY_SPREAD_PERCENTILE_MIN"), 35
)
DIVIDEND_BUY_PE_PERCENTILE_MAX = _env_float_any(
    ("DIVIDEND_BUY_PE_PERCENTILE_MAX", "BUY_PE_PERCENTILE_MAX"), 75
)
DIVIDEND_SPREAD_PERCENTILE_WINDOW = _env_int_any(
    ("DIVIDEND_SPREAD_PERCENTILE_WINDOW", "SPREAD_PERCENTILE_WINDOW"), 756
)
DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS = _env_int_any(
    ("DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS", "SPREAD_PERCENTILE_MIN_DAYS"), 60
)
DIVIDEND_SIGNAL_HISTORY_START = _env_str(
    "DIVIDEND_SIGNAL_HISTORY_START", "20180101"
)
DIVIDEND_BUY_MAX_ABOVE_LOW_PCT = _env_float_any(
    ("DIVIDEND_BUY_MAX_ABOVE_LOW_PCT",), 0.08
)
DIVIDEND_BUY_LOW_LOOKBACK_DAYS = _env_int_any(
    ("DIVIDEND_BUY_LOW_LOOKBACK_DAYS",), 60
)

_DIVIDEND_CFG_SUFFIX = {
    "buy_spread_min": "BUY_SPREAD_MIN",
    "buy_spread_percentile_min": "BUY_SPREAD_PERCENTILE_MIN",
    "buy_pe_percentile_max": "BUY_PE_PERCENTILE_MAX",
}

_DIVIDEND_GLOBAL_DEFAULTS = {
    "buy_spread_min": DIVIDEND_BUY_SPREAD_MIN,
    "buy_spread_percentile_min": DIVIDEND_BUY_SPREAD_PERCENTILE_MIN,
    "buy_pe_percentile_max": DIVIDEND_BUY_PE_PERCENTILE_MAX,
}

_DIVIDEND_PER_INDEX_DEFAULTS = {
    "930955": {
        "buy_spread_percentile_min": 37,
        "buy_pe_percentile_max": 75,
    },
    "H30269": {
        "buy_spread_percentile_min": 48,
        "buy_pe_percentile_max": 70,
    },
}


def get_dividend_signal_config(index_code):
    """读取单只红利指数的买入阈值（分指数默认 + 环境变量覆盖）。"""
    per_index = _DIVIDEND_PER_INDEX_DEFAULTS.get(index_code, {})
    cfg = {}
    for key, suffix in _DIVIDEND_CFG_SUFFIX.items():
        default = per_index.get(key, _DIVIDEND_GLOBAL_DEFAULTS[key])
        env_names = [f"DIVIDEND_{index_code}_{suffix}", f"DIVIDEND_{suffix}"]
        if key == "buy_spread_min":
            env_names.append("BUY_CONDITION_SPREAD")
        elif key == "buy_spread_percentile_min":
            env_names.append("BUY_SPREAD_PERCENTILE_MIN")
        elif key == "buy_pe_percentile_max":
            env_names.append("BUY_PE_PERCENTILE_MAX")
        cfg[key] = _env_float_any(tuple(env_names), default)

    max_above_default = per_index.get(
        "buy_max_above_low_pct", DIVIDEND_BUY_MAX_ABOVE_LOW_PCT
    )
    cfg["buy_max_above_low_pct"] = _env_float_any(
        (
            f"DIVIDEND_{index_code}_BUY_MAX_ABOVE_LOW_PCT",
            "DIVIDEND_BUY_MAX_ABOVE_LOW_PCT",
        ),
        max_above_default,
    )

    lookback_default = per_index.get(
        "buy_low_lookback_days", DIVIDEND_BUY_LOW_LOOKBACK_DAYS
    )
    cfg["buy_low_lookback_days"] = _env_int_any(
        (
            f"DIVIDEND_{index_code}_BUY_LOW_LOOKBACK_DAYS",
            "DIVIDEND_BUY_LOW_LOOKBACK_DAYS",
        ),
        lookback_default,
    )
    return cfg


# 兼容旧变量名
BUY_CONDITION_SPREAD = DIVIDEND_BUY_SPREAD_MIN
BUY_SPREAD_PERCENTILE_MIN = DIVIDEND_BUY_SPREAD_PERCENTILE_MIN
BUY_PE_PERCENTILE_MAX = DIVIDEND_BUY_PE_PERCENTILE_MAX
SPREAD_PERCENTILE_WINDOW = DIVIDEND_SPREAD_PERCENTILE_WINDOW
SPREAD_PERCENTILE_MIN_DAYS = DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS

# --- A 股宽基（A500 / 沪深300 / 中证1000，逻辑相同、阈值分指数）---
# 买入：股债利差分位达标 + PE 分位达标（股息率分位已取消）
CN_BROAD_BUY_SPREAD_PERCENTILE_MIN = _env_float(
    "CN_BROAD_BUY_SPREAD_PERCENTILE_MIN", 52
)
CN_BROAD_BUY_PE_PERCENTILE_MAX = _env_float("CN_BROAD_BUY_PE_PERCENTILE_MAX", 72)
CN_BROAD_BUY_PB_PERCENTILE_MAX = _env_float("CN_BROAD_BUY_PB_PERCENTILE_MAX", 62)
CN_BROAD_BUY_REQUIRE_SPREAD = _env_bool("CN_BROAD_BUY_REQUIRE_SPREAD", True)
CN_BROAD_BUY_MIN_APPLICABLE_CRITERIA = _env_int(
    "CN_BROAD_BUY_MIN_APPLICABLE_CRITERIA", 2
)
CN_BROAD_BUY_MIN_PASS_SCORE_FLOOR = _env_int(
    "CN_BROAD_BUY_MIN_PASS_SCORE_FLOOR", 3
)
CN_BROAD_PERCENTILE_WINDOW = _env_int("CN_BROAD_PERCENTILE_WINDOW", 2520)
CN_BROAD_PERCENTILE_MIN_DAYS = _env_int("CN_BROAD_PERCENTILE_MIN_DAYS", 120)
CN_BROAD_BUY_MAX_ABOVE_LOW_PCT = _env_float("CN_BROAD_BUY_MAX_ABOVE_LOW_PCT", 0.06)
CN_BROAD_BUY_LOW_LOOKBACK_DAYS = _env_int("CN_BROAD_BUY_LOW_LOOKBACK_DAYS", 60)
CN_BROAD_BUY_HIGH_LOOKBACK_DAYS = _env_int("CN_BROAD_BUY_HIGH_LOOKBACK_DAYS", 252)
CN_BROAD_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT = None
# 卖出：PE/PB 分位偏高、利差分位偏低或距近期低点涨幅过大（满足其一）
CN_BROAD_SELL_SPREAD_PERCENTILE_MAX = _env_float(
    "CN_BROAD_SELL_SPREAD_PERCENTILE_MAX", 25
)
CN_BROAD_SELL_PE_PERCENTILE_MIN = _env_float("CN_BROAD_SELL_PE_PERCENTILE_MIN", 85)
CN_BROAD_SELL_PB_PERCENTILE_MIN = _env_float("CN_BROAD_SELL_PB_PERCENTILE_MIN", 99)
CN_BROAD_SELL_MAX_ABOVE_LOW_PCT = _env_float("CN_BROAD_SELL_MAX_ABOVE_LOW_PCT", 0.20)

# 兼容旧变量名（A500）
A500_BUY_SPREAD_PERCENTILE_MIN = _env_float(
    "A500_BUY_SPREAD_PERCENTILE_MIN", CN_BROAD_BUY_SPREAD_PERCENTILE_MIN
)
A500_BUY_PE_PERCENTILE_MAX = _env_float(
    "A500_BUY_PE_PERCENTILE_MAX", CN_BROAD_BUY_PE_PERCENTILE_MAX
)
A500_BUY_PB_PERCENTILE_MAX = _env_float(
    "A500_BUY_PB_PERCENTILE_MAX", CN_BROAD_BUY_PB_PERCENTILE_MAX
)
A500_BUY_REQUIRE_SPREAD = _env_bool("A500_BUY_REQUIRE_SPREAD", CN_BROAD_BUY_REQUIRE_SPREAD)
A500_BUY_MIN_APPLICABLE_CRITERIA = _env_int(
    "A500_BUY_MIN_APPLICABLE_CRITERIA", CN_BROAD_BUY_MIN_APPLICABLE_CRITERIA
)
A500_BUY_MIN_PASS_SCORE_FLOOR = _env_int(
    "A500_BUY_MIN_PASS_SCORE_FLOOR", CN_BROAD_BUY_MIN_PASS_SCORE_FLOOR
)
A500_PERCENTILE_WINDOW = _env_int("A500_PERCENTILE_WINDOW", CN_BROAD_PERCENTILE_WINDOW)
A500_PERCENTILE_MIN_DAYS = _env_int("A500_PERCENTILE_MIN_DAYS", CN_BROAD_PERCENTILE_MIN_DAYS)
A500_BUY_MAX_ABOVE_LOW_PCT = _env_float(
    "A500_BUY_MAX_ABOVE_LOW_PCT", CN_BROAD_BUY_MAX_ABOVE_LOW_PCT
)
A500_BUY_LOW_LOOKBACK_DAYS = _env_int(
    "A500_BUY_LOW_LOOKBACK_DAYS", CN_BROAD_BUY_LOW_LOOKBACK_DAYS
)

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
    "sell_spread_percentile_max": "SELL_SPREAD_PERCENTILE_MAX",
    "sell_pe_percentile_min": "SELL_PE_PERCENTILE_MIN",
    "sell_pb_percentile_min": "SELL_PB_PERCENTILE_MIN",
    "sell_max_above_low_pct": "SELL_MAX_ABOVE_LOW_PCT",
}

_CN_BROAD_GLOBAL_DEFAULTS = {
    "buy_spread_percentile_min": CN_BROAD_BUY_SPREAD_PERCENTILE_MIN,
    "buy_pe_percentile_max": CN_BROAD_BUY_PE_PERCENTILE_MAX,
    "buy_pb_percentile_max": CN_BROAD_BUY_PB_PERCENTILE_MAX,
    "buy_require_spread": CN_BROAD_BUY_REQUIRE_SPREAD,
    "buy_min_applicable_criteria": CN_BROAD_BUY_MIN_APPLICABLE_CRITERIA,
    "buy_min_pass_score_floor": CN_BROAD_BUY_MIN_PASS_SCORE_FLOOR,
    "percentile_window": CN_BROAD_PERCENTILE_WINDOW,
    "percentile_min_days": CN_BROAD_PERCENTILE_MIN_DAYS,
    "buy_max_above_low_pct": CN_BROAD_BUY_MAX_ABOVE_LOW_PCT,
    "buy_low_lookback_days": CN_BROAD_BUY_LOW_LOOKBACK_DAYS,
    "buy_high_lookback_days": CN_BROAD_BUY_HIGH_LOOKBACK_DAYS,
    "buy_min_drawdown_from_high_pct": CN_BROAD_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
    "sell_spread_percentile_max": CN_BROAD_SELL_SPREAD_PERCENTILE_MAX,
    "sell_pe_percentile_min": CN_BROAD_SELL_PE_PERCENTILE_MIN,
    "sell_pb_percentile_min": CN_BROAD_SELL_PB_PERCENTILE_MIN,
    "sell_max_above_low_pct": CN_BROAD_SELL_MAX_ABOVE_LOW_PCT,
}

_CN_BROAD_PER_INDEX_DEFAULTS = {
    # 卖点：PE 分位偏高且（利差收敛或短期涨幅过大），避免熊市反弹误卖
    "000510": {
        "buy_spread_percentile_min": 58,
        "buy_pe_percentile_max": 70,
        "buy_max_above_low_pct": 0.07,
        "sell_spread_percentile_max": 20,
        "sell_pe_percentile_min": 95,
        "sell_max_above_low_pct": 0.22,
    },
    # 沪深300：2021 年 PE/利差已便宜但距 252 日高点仅回撤约 15%，需更深回撤再买
    "000300": {
        "buy_spread_percentile_min": 74,
        "buy_pe_percentile_max": 58,
        "buy_low_lookback_days": 120,
        "buy_max_above_low_pct": 0.03,
        "buy_high_lookback_days": 252,
        "buy_min_drawdown_from_high_pct": 0.18,
        "sell_spread_percentile_max": 25,
        "sell_pe_percentile_min": 85,
        "sell_max_above_low_pct": 0.18,
    },
    "000905": {
        "buy_spread_percentile_min": 66,
        "buy_pe_percentile_max": 65,
        "buy_max_above_low_pct": 0.055,
        "sell_spread_percentile_max": 22,
        "sell_pe_percentile_min": 95,
        "sell_max_above_low_pct": 0.24,
    },
    "000852": {
        "buy_spread_percentile_min": 65,
        "buy_pe_percentile_max": 67,
        "buy_max_above_low_pct": 0.065,
        "sell_spread_percentile_max": 25,
        "sell_pe_percentile_min": 88,
        "sell_max_above_low_pct": 0.22,
    },
    "000688": {
        "buy_spread_percentile_min": 66,
        "buy_pe_percentile_max": 62,
        "buy_max_above_low_pct": 0.055,
        "sell_spread_percentile_max": 22,
        "sell_pe_percentile_min": 95,
        "sell_max_above_low_pct": 0.25,
    },
}


def get_cn_broad_signal_config(index_code):
    """读取单只 A 股宽基指数的买入/卖出阈值（分指数默认 + 环境变量覆盖）。"""
    per_index = _CN_BROAD_PER_INDEX_DEFAULTS.get(index_code, {})
    cfg = {}
    for key, suffix in _CN_BROAD_CFG_SUFFIX.items():
        default = per_index.get(key, _CN_BROAD_GLOBAL_DEFAULTS[key])
        if key == "buy_require_spread":
            env_names = [
                f"CN_BROAD_{index_code}_{suffix}",
                f"CN_BROAD_{suffix}",
                f"A500_{suffix}",
            ]
            raw = os.environ.get(env_names[0]) or os.environ.get(env_names[1])
            if raw is None:
                raw = os.environ.get(env_names[2])
            if raw is None or raw == "":
                cfg[key] = default
            else:
                cfg[key] = raw.strip().lower() in ENV_BOOL_TRUE
            continue
        if key in ("buy_min_applicable_criteria", "buy_min_pass_score_floor"):
            env_names = [
                f"CN_BROAD_{index_code}_{suffix}",
                f"CN_BROAD_{suffix}",
                f"A500_{suffix}",
            ]
            cfg[key] = _env_int_any(tuple(env_names), default)
            continue
        if key in ("percentile_window", "percentile_min_days", "buy_low_lookback_days", "buy_high_lookback_days"):
            env_names = [
                f"CN_BROAD_{index_code}_{suffix}",
                f"CN_BROAD_{suffix}",
                f"A500_{suffix}",
            ]
            cfg[key] = _env_int_any(tuple(env_names), default)
            continue
        env_names = [
            f"CN_BROAD_{index_code}_{suffix}",
            f"CN_BROAD_{suffix}",
            f"A500_{suffix}",
        ]
        cfg[key] = _env_float_any(tuple(env_names), default)
    return cfg

# --- 创业板指（399006）---
# 买入：加权 PE/PB 分位偏低 + PEG(近5年增速) ≤ 阈值（三项须同时满足）
CYB_EXPECTED_GROWTH = _env_float("CYB_EXPECTED_GROWTH", 0.3906)
CYB_HISTORICAL_GROWTH = _env_float("CYB_HISTORICAL_GROWTH", 0.1663)
CYB_ROE_AVG = _env_float("CYB_ROE_AVG", 0.1229)
CYB_BUY_PE_PERCENTILE_MAX = _env_float("CYB_BUY_PE_PERCENTILE_MAX", 65)
CYB_BUY_PB_PERCENTILE_MAX = _env_float("CYB_BUY_PB_PERCENTILE_MAX", 48)
CYB_BUY_PEG_EXPECTED_MAX = _env_float("CYB_BUY_PEG_EXPECTED_MAX", 1.1)
CYB_BUY_PEG_HIST_MAX = _env_float("CYB_BUY_PEG_HIST_MAX", 2.6)
CYB_SELL_PE_PERCENTILE_MIN = _env_float("CYB_SELL_PE_PERCENTILE_MIN", 78)
CYB_SELL_PB_PERCENTILE_MIN = _env_float("CYB_SELL_PB_PERCENTILE_MIN", 78)
CYB_SELL_PEG_HIST_MIN = _env_float("CYB_SELL_PEG_HIST_MIN", 3.0)
CYB_SELL_COMBO_PE_PERCENTILE_MIN = _env_float("CYB_SELL_COMBO_PE_PERCENTILE_MIN", 60)
CYB_SELL_COMBO_PB_PERCENTILE_MIN = _env_float("CYB_SELL_COMBO_PB_PERCENTILE_MIN", 60)
CYB_PERCENTILE_WINDOW = _env_int("CYB_PERCENTILE_WINDOW", 2520)
CYB_DIV_PERCENTILE_WINDOW = _env_int("CYB_DIV_PERCENTILE_WINDOW", 1260)
CYB_PERCENTILE_MIN_DAYS = _env_int("CYB_PERCENTILE_MIN_DAYS", 120)
CYB_BUY_MAX_ABOVE_LOW_PCT = _env_float("CYB_BUY_MAX_ABOVE_LOW_PCT", 0.06)
CYB_BUY_LOW_LOOKBACK_DAYS = _env_int("CYB_BUY_LOW_LOOKBACK_DAYS", 60)

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
    "NDX_BUY_TRAILING_PE_PERCENTILE_MAX", 72
)
NDX_BUY_FORWARD_PE_PERCENTILE_MAX = _env_float(
    "NDX_BUY_FORWARD_PE_PERCENTILE_MAX", 75
)
NDX_BUY_PEG_FORWARD_MAX = _env_float("NDX_BUY_PEG_FORWARD_MAX", 1.35)
NDX_BUY_PEG_HIST_MAX = _env_float("NDX_BUY_PEG_HIST_MAX", 1.6)
NDX_BUY_RATE_PERCENTILE_MAX = _env_float("NDX_BUY_RATE_PERCENTILE_MAX", 92)
NDX_HISTORY_YEARS = _env_int("NDX_HISTORY_YEARS", 10)
NDX_PERCENTILE_WINDOW = _env_int("NDX_PERCENTILE_WINDOW", 120)
NDX_PERCENTILE_MIN_DAYS = _env_int("NDX_PERCENTILE_MIN_DAYS", 24)
NDX_DAILY_PERCENTILE_WINDOW = _env_int("NDX_DAILY_PERCENTILE_WINDOW", 2520)
NDX_DAILY_PERCENTILE_MIN_DAYS = _env_int("NDX_DAILY_PERCENTILE_MIN_DAYS", 252)
NDX_FRED_NETWORK_TIMEOUT = _env_int("NDX_FRED_NETWORK_TIMEOUT", 10)
NDX_BUY_MAX_ABOVE_LOW_PCT = _env_float("NDX_BUY_MAX_ABOVE_LOW_PCT", 0.10)
NDX_BUY_LOW_LOOKBACK_DAYS = _env_int("NDX_BUY_LOW_LOOKBACK_DAYS", 60)

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
    "SPX_BUY_TRAILING_PE_PERCENTILE_MAX", 72
)
SPX_BUY_FORWARD_PE_PERCENTILE_MAX = _env_float(
    "SPX_BUY_FORWARD_PE_PERCENTILE_MAX", 74
)
SPX_BUY_PEG_FORWARD_MAX = _env_float("SPX_BUY_PEG_FORWARD_MAX", 1.25)
SPX_BUY_PEG_HIST_MAX = _env_float("SPX_BUY_PEG_HIST_MAX", 1.5)
SPX_BUY_RATE_PERCENTILE_MAX = _env_float("SPX_BUY_RATE_PERCENTILE_MAX", 92)
SPX_HISTORY_YEARS = _env_int("SPX_HISTORY_YEARS", 10)
SPX_PERCENTILE_WINDOW = _env_int("SPX_PERCENTILE_WINDOW", 120)
SPX_PERCENTILE_MIN_DAYS = _env_int("SPX_PERCENTILE_MIN_DAYS", 24)
SPX_DAILY_PERCENTILE_WINDOW = _env_int("SPX_DAILY_PERCENTILE_WINDOW", 2520)
SPX_DAILY_PERCENTILE_MIN_DAYS = _env_int("SPX_DAILY_PERCENTILE_MIN_DAYS", 252)
SPX_FRED_NETWORK_TIMEOUT = _env_int("SPX_FRED_NETWORK_TIMEOUT", 10)
SPX_BUY_MAX_ABOVE_LOW_PCT = _env_float("SPX_BUY_MAX_ABOVE_LOW_PCT", 0.08)
SPX_BUY_LOW_LOOKBACK_DAYS = _env_int("SPX_BUY_LOW_LOOKBACK_DAYS", 60)

# --- 其他参考阈值（当前代码未用于主信号逻辑，保留可配置）---
STRONG_BUY_SPREAD = _env_float("STRONG_BUY_SPREAD", 0.035)
NORMAL_BUY_SPREAD = _env_float("NORMAL_BUY_SPREAD", 0.02)
SELL_SPREAD = _env_float("SELL_SPREAD", 0.01)
STRONG_SELL_SPREAD = _env_float("STRONG_SELL_SPREAD", 0.005)
PE_LOW_PERCENTILE = _env_float("PE_LOW_PERCENTILE", 30)
PE_HIGH_PERCENTILE = _env_float("PE_HIGH_PERCENTILE", 70)
# 无日度国债数据时按年回填（2024-09 起用接口真实数据）
BOND_YIELD_FALLBACK_BY_YEAR = {
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
