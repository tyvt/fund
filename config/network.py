"""网络请求与数据源 URL。"""

import os

from config.env import _env_float, _env_int, _env_str
from config.paths import CONFIG_FILE
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

