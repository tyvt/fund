# -*- coding: utf-8 -*-
"""全市场估值锚点（中证800 PE 历史分位）。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from dividend_lowvol_rotation.config import (
    CACHE_DIR,
    MARKET_VALUATION_INDEX,
    MARKET_VALUATION_PE_LOOKBACK_DAYS,
)
from data_cache import load_dataframe, save_dataframe
from market_data import compute_percentile


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"market_pe_{MARKET_VALUATION_INDEX}.csv"


def _normalize_start_end(start: str | None, end: str | None) -> tuple[str | None, str | None]:
    def _fmt(s: str | None) -> str | None:
        if not s:
            return None
        return s.replace("-", "")[:8]

    return _fmt(start), _fmt(end)


def load_market_pe_history(
    start: str | None = None,
    end: str | None = None,
    *,
    refresh: bool = False,
) -> pd.DataFrame:
    """指数 PE 长序列：官方指标 + 行情 rolling_pe 校准（与宽基模块一致）。"""
    path = _cache_path()
    cached = None if refresh else load_dataframe(path, parse_dates=["date"])
    start_fmt, end_fmt = _normalize_start_end(start, end)

    try:
        from cn_broad_data import build_cn_broad_valuation_history

        panel = build_cn_broad_valuation_history(
            MARKET_VALUATION_INDEX,
            start_date=start_fmt,
            end_date=end_fmt,
        )
    except Exception:
        panel = None

    if panel is None or panel.empty:
        return cached if cached is not None else pd.DataFrame()

    hist = panel[["date", "pe"]].copy()
    hist["date"] = pd.to_datetime(hist["date"])
    hist["pe"] = pd.to_numeric(hist["pe"], errors="coerce")
    hist = hist.dropna(subset=["date", "pe"]).sort_values("date")
    if cached is not None and not cached.empty:
        hist = (
            pd.concat([cached, hist], ignore_index=True)
            .drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
        )
    save_dataframe(path, hist)
    if start:
        hist = hist[hist["date"] >= pd.Timestamp(start)]
    if end:
        hist = hist[hist["date"] <= pd.Timestamp(end)]
    return hist.reset_index(drop=True)


def pe_percentile_as_of(
    as_of: pd.Timestamp,
    hist: pd.DataFrame | None = None,
    *,
    lookback_days: int = MARKET_VALUATION_PE_LOOKBACK_DAYS,
) -> tuple[float | None, float | None]:
    """返回 (当日 PE, 相对 lookback 窗口的历史分位 0~100)。"""
    if hist is None or hist.empty:
        hist = load_market_pe_history()
    if hist.empty:
        return None, None
    sub = hist[hist["date"] <= as_of].copy()
    if sub.empty:
        return None, None
    row = sub.iloc[-1]
    pe = float(row["pe"])
    window = sub.tail(lookback_days)["pe"]
    pct = compute_percentile(window, pe)
    return pe, pct


def valuation_regime(
    as_of: pd.Timestamp,
    hist: pd.DataFrame | None = None,
) -> dict:
    from dividend_lowvol_rotation.config import (
        MARKET_VALUATION_PE_PAUSE_PCT,
        MARKET_VALUATION_PE_TIGHT_PCT,
        MARKET_VALUATION_PAUSE_BUYS_ENABLED,
    )

    pe, pct = pe_percentile_as_of(as_of, hist)
    tight = pct is not None and pct >= MARKET_VALUATION_PE_TIGHT_PCT
    pause_buy = (
        MARKET_VALUATION_PAUSE_BUYS_ENABLED
        and pct is not None
        and pct >= MARKET_VALUATION_PE_PAUSE_PCT
    )
    return {
        "market_pe": pe,
        "market_pe_percentile": pct,
        "valuation_tight": tight,
        "pause_new_buys": pause_buy,
    }
