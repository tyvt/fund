"""Shared stock-code conversions used by RQAlpha data export tools."""

from __future__ import annotations


def normalize_stock_code(code: str | int) -> str:
    return str(code).strip().zfill(6)


def exchange_for_code(code: str | int) -> str | None:
    normalized = normalize_stock_code(code)
    if normalized.startswith("6"):
        return "XSHG"
    if normalized.startswith(("0", "3")):
        return "XSHE"
    if normalized.startswith(("8", "4")):
        return "BJSE"
    return None


def to_rqalpha_id(code: str | int) -> str | None:
    normalized = normalize_stock_code(code)
    exchange = exchange_for_code(normalized)
    return f"{normalized}.{exchange}" if exchange else None


def from_rqalpha_id(order_book_id: str) -> str:
    return normalize_stock_code(str(order_book_id).split(".")[0])
