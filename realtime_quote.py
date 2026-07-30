"""腾讯财经实时行情（免费、无需 API Key）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

import requests

from config import HEADERS, TENCENT_QUOTE_URL

# 指数代码 -> 腾讯行情代码（无实时源的指数不在此表）
TENCENT_SYMBOL_BY_INDEX: dict[str, str] = {
    "000510": "sh000510",
    "000300": "sh000300",
    "000905": "sh000905",
    "000852": "sh000852",
    "000688": "sh000688",
    "399006": "sz399006",
    "HSTECH": "hkHSTECH",
    "NDX": "usNDX",
    "SPX": "usINX",
}

_INDEX_BY_TENCENT = {v: k for k, v in TENCENT_SYMBOL_BY_INDEX.items()}


@dataclass(frozen=True)
class LiveQuote:
    index_code: str
    symbol: str
    price: float
    prev_close: float | None
    quote_time: str | None


def tencent_symbol_for_index(index_code: str) -> str | None:
    return TENCENT_SYMBOL_BY_INDEX.get(index_code)


def supported_live_index_codes() -> tuple[str, ...]:
    return tuple(TENCENT_SYMBOL_BY_INDEX)


def _parse_quote_line(line: str) -> LiveQuote | None:
    line = line.strip()
    if not line or "=\"" not in line:
        return None
    var_part, payload = line.split("=\"", 1)
    payload = payload.rstrip("\";\n\r")
    if not payload or payload == "pv_none_match=1":
        return None
    symbol = var_part.split("_", 1)[-1]
    index_code = _INDEX_BY_TENCENT.get(symbol)
    if index_code is None:
        return None
    fields = payload.split("~")
    if len(fields) < 4:
        return None
    try:
        price = float(fields[3])
    except (TypeError, ValueError):
        return None
    prev_close = None
    try:
        if fields[4]:
            prev_close = float(fields[4])
    except (TypeError, ValueError):
        prev_close = None
    quote_time = fields[30].strip() if len(fields) > 30 and fields[30] else None
    if price <= 0:
        return None
    return LiveQuote(
        index_code=index_code,
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        quote_time=quote_time,
    )


def fetch_live_quotes(index_codes: Iterable[str] | None = None) -> dict[str, LiveQuote]:
    """批量拉取实时行情，返回 {指数代码: LiveQuote}。"""
    if index_codes is None:
        symbols = list(TENCENT_SYMBOL_BY_INDEX.values())
    else:
        symbols = []
        for code in index_codes:
            sym = tencent_symbol_for_index(code)
            if sym:
                symbols.append(sym)
    if not symbols:
        return {}

    url = TENCENT_QUOTE_URL + ",".join(symbols)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="replace")
    except requests.RequestException:
        return {}

    out: dict[str, LiveQuote] = {}
    for chunk in re.split(r";[\r\n]*", text):
        quote = _parse_quote_line(chunk)
        if quote is not None:
            out[quote.index_code] = quote
    return out


def fetch_live_quote(index_code: str) -> LiveQuote | None:
    return fetch_live_quotes([index_code]).get(index_code)
