"""美股指数（纳指 100 / 标普 500）估值数据拉取与历史分位计算。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO
from typing import Any

import pandas as pd
import requests

import config
from config import (
    BUY_RANGE_LOOKBACK_DAYS,
    BUY_TREND_MA_DAYS,
    BUY_TREND_SLOPE_LOOKBACK_DAYS,
    FRED_NASDAQ100_SERIES,
    HEADERS,
    NDX_FORWARD_PE_URL,
    NDX_INDEX,
    REQUEST_TIMEOUT,
    SPX_FORWARD_PE_URL,
    SPX_INDEX,
    US_INDEX_KEYS,
)
from data_cache import (
    get_or_fetch_us_dataframe,
    get_or_fetch_us_json,
    get_or_fetch_us_text,
    us_cache_path,
)
from market_data import compute_percentile
from price_position import (
    attach_ma_trend,
    attach_pct_above_low,
    attach_pct_below_high,
    attach_year_range_position,
    row_price_position_fields,
)

FRED_US10Y_SERIES = "DGS10"
NASDAQ_ETF_SUMMARY_URL = (
    "https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=etf"
)

US_INDEX_SPECS: dict[str, dict[str, Any]] = {
    "ndx": {
        "index": NDX_INDEX,
        "forward_pe_url": NDX_FORWARD_PE_URL,
        "pe_cache": "ndx_forward_pe.json",
        "fred_price_series": FRED_NASDAQ100_SERIES,
        "akshare_symbol": ".NDX",
        "price_cache": "ndx_price_akshare.csv",
        "dividend_proxy": config.NDX_DIVIDEND_PROXY_SYMBOL,
        "dividend_cache": "ndx_qqq_dividend_yield.json",
        "hist_growth_min_months": lambda years: years * 6,
        "runtime_error": "无法构建纳斯达克 100 日频估值序列",
    },
    "spx": {
        "index": SPX_INDEX,
        "forward_pe_url": SPX_FORWARD_PE_URL,
        "pe_cache": "spx_forward_pe.json",
        "fred_price_series": "SP500",
        "akshare_symbol": ".INX",
        "price_cache": "spx_price_akshare.csv",
        "dividend_proxy": config.SPX_DIVIDEND_PROXY_SYMBOL,
        "dividend_cache": "spx_spy_dividend_yield.json",
        "hist_growth_min_months": lambda years: years * 3,
        "runtime_error": "无法构建标普 500 日频估值序列",
    },
}


def _cfg(key: str, suffix: str):
    return getattr(config, f"{key.upper()}_{suffix}")


def _spec(key: str) -> dict[str, Any]:
    if key not in US_INDEX_SPECS:
        raise ValueError(f"未知美股指数: {key}，可选 {', '.join(US_INDEX_KEYS)}")
    return US_INDEX_SPECS[key]


def _history_start(key: str):
    years = _cfg(key, "HISTORY_YEARS")
    return pd.Timestamp(date.today()) - pd.DateOffset(years=years)


def _filter_since(frame, date_col="date", *, key: str):
    if frame is None or frame.empty:
        return frame
    return frame[frame[date_col] >= _history_start(key)].copy()


def _fetch_json(url, timeout=REQUEST_TIMEOUT):
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_fred_series(series_id, start_date=None, *, allow_network=True):
    """从 FRED 拉取 CSV；当日已缓存则直接读取。"""
    cache_name = f"fred_{series_id}.csv"
    network_timeout = max(_cfg("ndx", "FRED_NETWORK_TIMEOUT"), REQUEST_TIMEOUT)

    def _download():
        response = requests.get(
            config.fred_csv_url(series_id),
            headers=HEADERS,
            timeout=network_timeout,
        )
        response.raise_for_status()
        return response.text

    if allow_network:
        text = get_or_fetch_us_text(cache_name, _download)
    else:
        path = us_cache_path(cache_name)
        if not path.exists():
            raise RuntimeError(f"缺少 FRED {series_id} 本地缓存")
        text = path.read_text(encoding="utf-8")

    frame = pd.read_csv(StringIO(text))
    value_col = [column for column in frame.columns if column != "observation_date"][0]
    frame = frame.rename(columns={"observation_date": "date", value_col: "value"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["date", "value"]).sort_values("date")
    if start_date is None:
        start_date = _history_start("ndx")
    frame = frame[frame["date"] >= pd.Timestamp(start_date)]
    return frame.reset_index(drop=True)


def _fetch_us10y_from_akshare():
    def _download():
        import akshare as ak

        raw = ak.bond_zh_us_rate()
        out = raw.rename(columns={"日期": "date", "美国国债收益率10年": "us10y"})
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["us10y"] = pd.to_numeric(out["us10y"], errors="coerce") / 100
        out = out.dropna(subset=["date", "us10y"])
        return out[["date", "us10y"]].sort_values("date").reset_index(drop=True)

    return get_or_fetch_us_dataframe("us10y_akshare.csv", _download)


def fetch_us10y_history(*, key: str = "ndx"):
    """美国 10 年期国债收益率。"""
    start = _history_start(key)
    try:
        history = fetch_fred_series(FRED_US10Y_SERIES, start_date=start)
        history["value"] = history["value"] / 100
        return history.rename(columns={"value": "us10y"})
    except (requests.RequestException, RuntimeError):
        out = _fetch_us10y_from_akshare()
        return out[out["date"] >= start].reset_index(drop=True)


def fetch_pe_payload(key: str):
    spec = _spec(key)
    payload = get_or_fetch_us_json(
        spec["pe_cache"],
        lambda: _fetch_json(
            spec["forward_pe_url"],
            timeout=_cfg(key, "FRED_NETWORK_TIMEOUT"),
        ),
    )
    current = payload.get("current") or {}
    trailing = pd.DataFrame(payload.get("trailing") or [])
    forward = pd.DataFrame(payload.get("forward") or [])
    if trailing.empty and forward.empty:
        raise RuntimeError(f"无法获取{spec['index']['name']} PE 数据")

    for frame in (trailing, forward):
        if frame.empty:
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        frame.dropna(subset=["date", "value"], inplace=True)
        frame.sort_values("date", inplace=True)

    trailing = _filter_since(trailing, key=key)
    forward = _filter_since(forward, key=key)
    return {
        "updated": payload.get("updated"),
        "source": payload.get("source"),
        "current": current,
        "trailing": trailing.reset_index(drop=True),
        "forward": forward.reset_index(drop=True),
    }


def _fetch_price_from_akshare(key: str):
    spec = _spec(key)

    def _download():
        import akshare as ak

        frame = ak.index_us_stock_sina(symbol=spec["akshare_symbol"])
        out = frame.rename(columns={"date": "date", "close": "close"})
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        return out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    return get_or_fetch_us_dataframe(spec["price_cache"], _download)


def fetch_price_history(key: str):
    spec = _spec(key)
    start = _history_start(key)
    try:
        history = fetch_fred_series(spec["fred_price_series"], start_date=start)
        return history.rename(columns={"value": "close"})
    except (requests.RequestException, RuntimeError):
        out = _fetch_price_from_akshare(key)
        return out[out["date"] >= start].reset_index(drop=True)


def fetch_dividend_yield_proxy(key: str):
    spec = _spec(key)

    def _fetch():
        url = NASDAQ_ETF_SUMMARY_URL.format(symbol=spec["dividend_proxy"])
        payload = _fetch_json(url, timeout=_cfg(key, "FRED_NETWORK_TIMEOUT"))
        summary = (payload.get("data") or {}).get("summaryData") or {}
        raw = (summary.get("Yield") or {}).get("value")
        if not raw:
            return {"yield": None}
        text = str(raw).strip().replace("%", "")
        return {"yield": float(text) / 100}

    try:
        data = get_or_fetch_us_json(spec["dividend_cache"], _fetch)
        return data.get("yield")
    except requests.RequestException:
        return None


def implied_earnings_growth(trailing_pe, forward_pe):
    if trailing_pe is None or forward_pe is None or forward_pe <= 0:
        return None
    return trailing_pe / forward_pe - 1


def build_valuation_panel(key: str):
    with ThreadPoolExecutor(max_workers=3) as executor:
        pe_future = executor.submit(fetch_pe_payload, key)
        price_future = executor.submit(fetch_price_history, key)
        us10y_future = executor.submit(fetch_us10y_history, key=key)
        pe_payload = pe_future.result()
        prices = price_future.result()
        us10y = us10y_future.result()

    forward = pe_payload["forward"].copy()
    forward = forward.rename(columns={"value": "forward_pe"})
    forward["month"] = forward["date"].dt.to_period("M")

    prices["month"] = prices["date"].dt.to_period("M")
    month_prices = (
        prices.sort_values("date")
        .groupby("month", as_index=False)
        .last()[["month", "date", "close"]]
    )

    panel = forward.merge(month_prices, on="month", how="left", suffixes=("_pe", "_px"))
    panel = panel.dropna(subset=["forward_pe", "close"])
    panel["implied_earnings"] = panel["close"] / panel["forward_pe"]
    panel["earnings_yield"] = 1 / panel["forward_pe"]
    panel = panel.sort_values("date_pe").reset_index(drop=True)

    panel = pd.merge_asof(
        panel.sort_values("date_pe"),
        us10y.sort_values("date").rename(columns={"date": "rate_date"}),
        left_on="date_pe",
        right_on="rate_date",
        direction="backward",
    )
    panel["date"] = panel["date_pe"]
    return panel, pe_payload


def compute_historical_earnings_growth(panel, key: str, years=5):
    spec = _spec(key)
    min_rows = spec["hist_growth_min_months"](years)
    if panel is None or len(panel) < min_rows:
        return None
    latest = panel.iloc[-1]
    target = latest["date"] - pd.DateOffset(years=years)
    past = panel[panel["date"] <= target]
    if past.empty:
        return None
    start = past.iloc[-1]
    if start["implied_earnings"] <= 0 or latest["implied_earnings"] <= 0:
        return None
    elapsed_years = (latest["date"] - start["date"]).days / 365.25
    if elapsed_years <= 0:
        return None
    return (latest["implied_earnings"] / start["implied_earnings"]) ** (1 / elapsed_years) - 1


def attach_percentiles(panel, key: str):
    if panel is None or panel.empty:
        return None

    window = _cfg(key, "PERCENTILE_WINDOW")
    min_days = _cfg(key, "PERCENTILE_MIN_DAYS")
    out = panel.copy()
    forward_pcts, rate_pcts = [], []
    for idx in range(len(out)):
        if idx < min_days:
            forward_pcts.append(None)
            rate_pcts.append(None)
            continue
        start = max(0, idx - window)
        forward_pcts.append(
            compute_percentile(
                out["forward_pe"].iloc[start:idx],
                out["forward_pe"].iloc[idx],
            )
        )
        rate_pcts.append(
            compute_percentile(
                out["us10y"].iloc[start:idx],
                out["us10y"].iloc[idx],
            )
        )

    out["forward_pe_percentile"] = forward_pcts
    out["us10y_percentile"] = rate_pcts
    return out


def attach_daily_percentiles(panel, key: str):
    if panel is None or panel.empty:
        return None

    window = _cfg(key, "DAILY_PERCENTILE_WINDOW")
    min_days = _cfg(key, "DAILY_PERCENTILE_MIN_DAYS")
    out = panel.sort_values("date").reset_index(drop=True).copy()
    forward_pcts, rate_pcts, trailing_pcts = [], [], []
    for idx in range(len(out)):
        if idx < min_days:
            forward_pcts.append(None)
            rate_pcts.append(None)
            trailing_pcts.append(None)
            continue
        start = max(0, idx - window)
        history = out.iloc[start:idx]
        forward_pcts.append(
            compute_percentile(
                history["forward_pe"].dropna(),
                out["forward_pe"].iloc[idx],
            )
            if pd.notna(out["forward_pe"].iloc[idx])
            else None
        )
        rate_pcts.append(
            compute_percentile(
                history["us10y"].dropna(),
                out["us10y"].iloc[idx],
            )
            if pd.notna(out["us10y"].iloc[idx])
            else None
        )
        trailing_pcts.append(
            compute_percentile(
                history["trailing_pe"].dropna(),
                out["trailing_pe"].iloc[idx],
            )
            if pd.notna(out["trailing_pe"].iloc[idx])
            else None
        )

    out["forward_pe_percentile"] = forward_pcts
    out["us10y_percentile"] = rate_pcts
    out["trailing_pe_percentile"] = trailing_pcts
    return out


def _scale_pe_by_price(panel, pe_col, anchor_date_col, prices):
    if pe_col not in panel.columns or anchor_date_col not in panel.columns:
        return panel
    anchor = prices.rename(
        columns={"date": anchor_date_col, "close": f"close_at_{anchor_date_col}"}
    )
    panel = panel.merge(
        anchor[[anchor_date_col, f"close_at_{anchor_date_col}"]],
        on=anchor_date_col,
        how="left",
    )
    anchor_close = panel[f"close_at_{anchor_date_col}"]
    valid = (
        panel[pe_col].notna()
        & anchor_close.notna()
        & (anchor_close > 0)
        & panel["close"].notna()
    )
    panel.loc[valid, pe_col] = panel.loc[valid, pe_col] * (
        panel.loc[valid, "close"] / anchor_close.loc[valid]
    )
    return panel.drop(columns=[f"close_at_{anchor_date_col}"], errors="ignore")


def build_daily_valuation_panel(key: str):
    with ThreadPoolExecutor(max_workers=3) as executor:
        pe_future = executor.submit(fetch_pe_payload, key)
        price_future = executor.submit(fetch_price_history, key)
        us10y_future = executor.submit(fetch_us10y_history, key=key)
        pe_payload = pe_future.result()
        prices = price_future.result()
        us10y = us10y_future.result()

    daily = prices[["date", "close"]].sort_values("date").reset_index(drop=True)
    daily = pd.merge_asof(
        daily,
        us10y.sort_values("date"),
        on="date",
        direction="backward",
    )

    forward = pe_payload["forward"].copy()
    if not forward.empty:
        forward = forward.rename(columns={"value": "forward_pe", "date": "fwd_date"})
        forward = forward.sort_values("fwd_date")
        daily = pd.merge_asof(
            daily,
            forward[["fwd_date", "forward_pe"]],
            left_on="date",
            right_on="fwd_date",
            direction="backward",
        )

    trailing = pe_payload["trailing"].copy()
    if not trailing.empty:
        trailing = trailing.rename(columns={"value": "trailing_pe", "date": "trail_date"})
        trailing = trailing.sort_values("trail_date")
        daily = pd.merge_asof(
            daily,
            trailing[["trail_date", "trailing_pe"]],
            left_on="date",
            right_on="trail_date",
            direction="backward",
        )

    daily = _scale_pe_by_price(daily, "forward_pe", "fwd_date", prices)
    daily = _scale_pe_by_price(daily, "trailing_pe", "trail_date", prices)
    daily["implied_growth"] = daily.apply(
        lambda row: implied_earnings_growth(row.get("trailing_pe"), row.get("forward_pe")),
        axis=1,
    )
    daily = attach_daily_percentiles(daily, key)
    daily = attach_pct_above_low(daily, lookback_days=_cfg(key, "BUY_LOW_LOOKBACK_DAYS"))
    daily = attach_pct_below_high(
        daily, lookback_days=_cfg(key, "BUY_HIGH_LOOKBACK_DAYS")
    )
    daily = attach_year_range_position(daily, lookback_days=BUY_RANGE_LOOKBACK_DAYS)
    daily = attach_ma_trend(
        daily,
        ma_days=BUY_TREND_MA_DAYS,
        slope_lookback=BUY_TREND_SLOPE_LOOKBACK_DAYS,
    )
    return daily, pe_payload


def fetch_snapshot(key: str, expected_growth=None):
    spec = _spec(key)
    daily, pe_payload = build_daily_valuation_panel(key)
    if daily is None or daily.empty:
        raise RuntimeError(spec["runtime_error"])

    month_panel, _ = build_valuation_panel(key)
    hist_growth = compute_historical_earnings_growth(month_panel, key)

    latest = daily.iloc[-1]
    trailing_pe = (
        float(latest["trailing_pe"])
        if pd.notna(latest.get("trailing_pe"))
        else None
    )
    forward_pe = (
        float(latest["forward_pe"])
        if pd.notna(latest.get("forward_pe"))
        else None
    )
    implied_growth = (
        float(latest["implied_growth"])
        if pd.notna(latest.get("implied_growth"))
        else implied_earnings_growth(trailing_pe, forward_pe)
    )

    trailing_history = pe_payload["trailing"]
    trailing_pe_percentile = latest.get("trailing_pe_percentile")
    if trailing_pe_percentile is None and (
        not trailing_history.empty
        and trailing_pe is not None
        and len(trailing_history) >= 20
    ):
        trailing_pe_percentile = compute_percentile(
            trailing_history["value"], trailing_pe
        )

    index_meta = spec["index"]
    snapshot = {
        "code": index_meta["code"],
        "name": index_meta["name"],
        "us_index_key": key,
        "date": pd.Timestamp(latest["date"]).date(),
        "trailing_pe": trailing_pe,
        "forward_pe": forward_pe,
        "trailing_pe_percentile": trailing_pe_percentile,
        "forward_pe_percentile": latest.get("forward_pe_percentile"),
        "implied_growth": implied_growth,
        "historical_growth": hist_growth,
        "expected_growth": expected_growth,
        "dividend_yield": fetch_dividend_yield_proxy(key),
        "us10y": float(latest["us10y"]) if pd.notna(latest.get("us10y")) else None,
        "us10y_percentile": latest.get("us10y_percentile"),
        "pct_above_low": (
            float(latest["pct_above_low"])
            if pd.notna(latest.get("pct_above_low"))
            else None
        ),
        "pct_below_high": (
            float(latest["pct_below_high"])
            if pd.notna(latest.get("pct_below_high"))
            else None
        ),
        "year_range_position": (
            float(latest["year_range_position"])
            if pd.notna(latest.get("year_range_position"))
            else None
        ),
        "ma_slope_pct": (
            float(latest["ma_slope_pct"])
            if pd.notna(latest.get("ma_slope_pct"))
            else None
        ),
        "history_years": _cfg(key, "HISTORY_YEARS"),
        "history_days": int(daily["forward_pe"].notna().sum()),
        "trailing_history_days": int(len(trailing_history)),
        "daily_history_days": int(len(daily)),
        "panel": daily,
        "pe_source": pe_payload.get("source"),
        "high_lookback_days": _cfg(key, "BUY_HIGH_LOOKBACK_DAYS"),
        **row_price_position_fields(latest),
    }
    from us_index_signal import resolve_expected_growth

    snapshot["expected_growth"] = resolve_expected_growth(key, snapshot)
    return snapshot
