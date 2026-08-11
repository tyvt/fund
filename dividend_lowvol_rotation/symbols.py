"""A 股代码与行情符号转换。"""

from __future__ import annotations

import re


def normalize_stock_code(code: str | int) -> str:
    return str(code).strip().zfill(6)


def to_baostock_code(code: str | int) -> str | None:
    c = normalize_stock_code(code)
    if c.startswith("6"):
        return f"sh.{c}"
    if c.startswith(("0", "3")):
        return f"sz.{c}"
    return None


def to_tencent_symbol(code: str | int) -> str | None:
    c = normalize_stock_code(code)
    if c.startswith("6"):
        return f"sh{c}"
    if c.startswith(("0", "3")):
        return f"sz{c}"
    return None


def is_excluded_name(name: str | None) -> bool:
    if not name:
        return False
    upper = str(name).upper()
    if "ST" in upper or upper.startswith("*"):
        return True
    if "退" in str(name):
        return True
    return False


def parse_holdings_text(text: str) -> list[str]:
    """解析持仓列表：每行一个代码，支持逗号/空格分隔。"""
    codes: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in re.split(r"[\s,;]+", line):
            part = part.strip()
            if part:
                codes.append(normalize_stock_code(part))
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out
