"""A 股 6 位代码与 RQAlpha order_book_id 互转。"""

from __future__ import annotations

from dividend_lowvol_rotation.symbols import normalize_stock_code


def to_rqalpha_id(code: str | int) -> str | None:
    """000001 -> 000001.XSHE；600000 -> 600000.XSHG。"""
    c = normalize_stock_code(code)
    if c.startswith("6"):
        return f"{c}.XSHG"
    if c.startswith(("0", "3")):
        return f"{c}.XSHE"
    return None


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
