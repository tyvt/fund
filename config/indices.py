"""指数元数据与筛选。"""

KC50_INDEX = {"code": "000688", "name": "科创50"}
CYB_INDEX = {"code": "399006", "name": "创业板指"}
NDX_INDEX = {"code": "NDX", "name": "纳斯达克100"}
NDX_MARKET_DATA_START = "2010-01-01"

CN_BROAD_INDICES = [
    KC50_INDEX,
]
US_INDEX_KEYS = ("ndx",)

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
