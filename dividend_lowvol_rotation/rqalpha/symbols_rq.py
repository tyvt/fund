"""A 股 6 位代码与 RQAlpha order_book_id 互转。"""

from __future__ import annotations

from dividend_lowvol_rotation.symbols import normalize_stock_code

# RQAlpha 交易所后缀
_EXCHANGE_SUFFIX = {
    "XSHG": "6",
    "XSHE": ("0", "3"),
    "BJSE": ("8", "4"),
}


def exchange_for_code(code: str | int) -> str | None:
    """返回 XSHG / XSHE / BJSE。"""
    c = normalize_stock_code(code)
    if c.startswith("6"):
        return "XSHG"
    if c.startswith(("0", "3")):
        return "XSHE"
    if c.startswith(("8", "4")):
        return "BJSE"
    return None


def to_rqalpha_id(code: str | int) -> str | None:
    """000001 -> 000001.XSHE；600000 -> 600000.XSHG；830001 -> 830001.BJSE。"""
    c = normalize_stock_code(code)
    exchange = exchange_for_code(c)
    if not exchange:
        return None
    return f"{c}.{exchange}"


def from_rqalpha_id(order_book_id: str) -> str:
    """000001.XSHE -> 000001。"""
    return normalize_stock_code(str(order_book_id).split(".")[0])


def to_rqalpha_ids(codes: list[str]) -> list[str]:
    out: list[str] = []
    for code in codes:
        obid = to_rqalpha_id(code)
        if obid:
            out.append(obid)
    return out


def rqalpha_mappable_codes(codes: list[str]) -> list[str]:
    """过滤出可映射到 RQAlpha order_book_id 的 6 位代码。"""
    return [normalize_stock_code(c) for c in codes if to_rqalpha_id(c)]
