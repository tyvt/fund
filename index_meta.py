"""各指数基日与数据拉取范围（用于全历史缓存与回测）。"""

from __future__ import annotations

from config import (
    A50_MARKET_DATA_START,
    A500_MARKET_DATA_START,
    CN_BROAD_INDICES,
    CYB_INDEX,
    HSTECH_INDEX,
    HSTECH_MARKET_DATA_START,
    INDICES,
    NDX_INDEX,
    NDX_MARKET_DATA_START,
    SPX_INDEX,
    SPX_MARKET_DATA_START,
    US_INDEX_KEYS,
    get_dividend_total_return_code,
)

# 行情/估值缓存起点（YYYYMMDD，中证 perf API 格式）
INDEX_BASE_DATES: dict[str, str] = {
    "930955": "20170505",
    "H30269": "20081231",
    "H20955": "20170505",
    "H20269": "20081231",
    "000510": A500_MARKET_DATA_START.replace("-", ""),
    "000016": "20040102",
    "000300": "20050408",
    "000905": "20070115",
    "000852": "20141017",
    "930050": A50_MARKET_DATA_START.replace("-", ""),
    "000903": "20060529",
    "000688": "20200723",
    "399006": "20100601",
    "HSTECH": HSTECH_MARKET_DATA_START.replace("-", ""),
    "NDX": NDX_MARKET_DATA_START.replace("-", ""),
    "SPX": SPX_MARKET_DATA_START.replace("-", ""),
}


def get_index_base_date(code: str) -> str | None:
    """返回指数基日 YYYYMMDD。"""
    return INDEX_BASE_DATES.get(code.upper() if code != "HSTECH" else code)


def get_index_base_date_iso(code: str) -> str | None:
    """返回指数基日 YYYY-MM-DD。"""
    raw = get_index_base_date(code)
    if not raw or len(raw) != 8:
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def iter_tracked_csindex_perf_codes() -> list[str]:
    """需拉取中证 perf API 的指数代码（含红利全收益）。"""
    codes: list[str] = []
    for item in INDICES:
        codes.append(item["code"])
        tr = get_dividend_total_return_code(item["code"])
        if tr:
            codes.append(tr)
    for item in CN_BROAD_INDICES:
        codes.append(item["code"])
    return list(dict.fromkeys(codes))


def iter_tracked_index_labels() -> list[tuple[str, str]]:
    """(代码, 名称) 列表，供同步脚本展示。"""
    labels = [(i["code"], i["name"]) for i in INDICES]
    for item in INDICES:
        tr = get_dividend_total_return_code(item["code"])
        if tr:
            labels.append((tr, f"{item['name']}(全收益)"))
    labels.extend((i["code"], i["name"]) for i in CN_BROAD_INDICES)
    labels.append((CYB_INDEX["code"], CYB_INDEX["name"]))
    labels.append((HSTECH_INDEX["code"], HSTECH_INDEX["name"]))
    labels.append((NDX_INDEX["code"], NDX_INDEX["name"]))
    labels.append((SPX_INDEX["code"], SPX_INDEX["name"]))
    return labels


def us_index_key_for_code(code: str) -> str | None:
    if code.upper() == NDX_INDEX["code"]:
        return "ndx"
    if code.upper() == SPX_INDEX["code"]:
        return "spx"
    return None


def all_us_keys() -> list[str]:
    return list(US_INDEX_KEYS)
