"""腾讯财经 A 股实时行情（免费、无需 API Key）。"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import requests

from config import HEADERS, TENCENT_QUOTE_URL
from dividend_lowvol_rotation.config import TENCENT_QUOTE_BATCH
from dividend_lowvol_rotation.symbols import normalize_stock_code, to_tencent_symbol


@dataclass(frozen=True)
class StockQuote:
    code: str
    name: str
    price: float
    prev_close: float | None
    quote_time: str | None


def _parse_line(line: str) -> StockQuote | None:
    line = line.strip()
    if not line or "=\"" not in line:
        return None
    var_part, payload = line.split("=\"", 1)
    payload = payload.rstrip("\";\n\r")
    if not payload or payload == "pv_none_match=1":
        return None
    symbol = var_part.split("_", 1)[-1]
    if not symbol.startswith(("sh", "sz")):
        return None
    code = symbol[2:]
    fields = payload.split("~")
    if len(fields) < 5:
        return None
    try:
        price = float(fields[3])
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    prev_close = None
    try:
        if fields[4]:
            prev_close = float(fields[4])
    except (TypeError, ValueError):
        prev_close = None
    quote_time = fields[30].strip() if len(fields) > 30 and fields[30] else None
    return StockQuote(
        code=normalize_stock_code(code),
        name=fields[1] if len(fields) > 1 else "",
        price=price,
        prev_close=prev_close,
        quote_time=quote_time,
    )


def fetch_stock_quotes(codes: list[str], batch_size: int = TENCENT_QUOTE_BATCH) -> dict[str, StockQuote]:
    symbols: list[str] = []
    code_by_symbol: dict[str, str] = {}
    for code in codes:
        sym = to_tencent_symbol(code)
        if sym:
            symbols.append(sym)
            code_by_symbol[sym] = normalize_stock_code(code)
    if not symbols:
        return {}

    results: dict[str, StockQuote] = {}
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i : i + batch_size]
        url = TENCENT_QUOTE_URL + ",".join(chunk)
        text = None
        for attempt in range(2):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                text = resp.content.decode("gbk", errors="replace")
                break
            except requests.RequestException:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
        if not text:
            continue
        for line in re.split(r";\s*", text):
            quote = _parse_line(line)
            if quote is not None:
                results[quote.code] = quote
        if i + batch_size < len(symbols):
            time.sleep(0.05)
    return results
