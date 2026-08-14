"""指数元数据与筛选。"""

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
