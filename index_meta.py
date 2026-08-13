"""各指数基日与数据拉取范围（用于全历史缓存与回测）。"""

from __future__ import annotations

from config import (
    CN_BROAD_INDICES,
    CYB_INDEX,
    INDICES,
    NDX_INDEX,
    NDX_MARKET_DATA_START,
    SPX_INDEX,
    SPX_MARKET_DATA_START,
    get_dividend_total_return_code,
)

# 行情/估值缓存起点（YYYYMMDD，中证 perf API 格式）
INDEX_BASE_DATES: dict[str, str] = {
    "H30269": "20081231",
    "H20269": "20081231",
    "000300": "20050408",
    "000906": "20070115",
    "000852": "20141017",
    "000688": "20200723",
    "399006": "20100601",
    "NDX": NDX_MARKET_DATA_START.replace("-", ""),
    "SPX": SPX_MARKET_DATA_START.replace("-", ""),
}


def get_index_base_date(code: str) -> str | None:
    """返回指数基日 YYYYMMDD。"""
    return INDEX_BASE_DATES.get(code.upper())


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


# 策略回测用、不在 INDICES/CN_BROAD 内的中证 perf 指数
EXTRA_CSINDEX_PERF_CODES = ("000300", "000906")


def iter_extra_csindex_perf_codes() -> list[str]:
    """策略模块额外依赖的中证 perf 指数（Beta 基准、全市场 PE 等）。"""
    tracked = set(iter_tracked_csindex_perf_codes())
    return [c for c in EXTRA_CSINDEX_PERF_CODES if c not in tracked]


def iter_all_csindex_perf_codes() -> list[str]:
    return list(dict.fromkeys([*iter_tracked_csindex_perf_codes(), *iter_extra_csindex_perf_codes()]))


def iter_tracked_index_labels() -> list[tuple[str, str]]:
    """(代码, 名称) 列表，供同步脚本展示。"""
    labels = [(i["code"], i["name"]) for i in INDICES]
    for item in INDICES:
        tr = get_dividend_total_return_code(item["code"])
        if tr:
            labels.append((tr, f"{item['name']}(全收益)"))
    labels.extend((i["code"], i["name"]) for i in CN_BROAD_INDICES)
    labels.append((CYB_INDEX["code"], CYB_INDEX["name"]))
    labels.append((NDX_INDEX["code"], NDX_INDEX["name"]))
    labels.append((SPX_INDEX["code"], SPX_INDEX["name"]))
    return labels
