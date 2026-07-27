"""纳斯达克 100 估值数据拉取与历史分位计算。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import StringIO
from pathlib import Path
import time

import pandas as pd
import requests

from config import (
    FRED_NASDAQ100_SERIES,
    HEADERS,
    NDX_BUY_LOW_LOOKBACK_DAYS,
    NDX_DAILY_PERCENTILE_MIN_DAYS,
    NDX_DAILY_PERCENTILE_WINDOW,
    NDX_DIVIDEND_PROXY_SYMBOL,
    NDX_FORWARD_PE_URL,
    NDX_FRED_NETWORK_TIMEOUT,
    NDX_HISTORY_YEARS,
    NDX_PERCENTILE_MIN_DAYS,
    NDX_PERCENTILE_WINDOW,
    NDX_INDEX,
    PROJECT_DIR,
    REQUEST_TIMEOUT,
    fred_csv_url,
)
from market_data import compute_percentile
from price_position import attach_pct_above_low


NDX_CODE = NDX_INDEX["code"]
NDX_NAME = NDX_INDEX["name"]
FRED_US10Y_SERIES = "DGS10"
NASDAQ_QQQ_SUMMARY_URL = (
    "https://api.nasdaq.com/api/quote/{symbol}/summary?assetclass=etf"
)
CACHE_DIR = PROJECT_DIR / "logs" / "us_index_cache"


def _cache_path(name):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _history_start():
    return pd.Timestamp(date.today()) - pd.DateOffset(years=NDX_HISTORY_YEARS)


def _filter_since(frame, date_col="date"):
    if frame is None or frame.empty:
        return frame
    return frame[frame[date_col] >= _history_start()].copy()


def _fetch_json(url, timeout=REQUEST_TIMEOUT):
    response = requests.get(url, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _should_refresh_cache(cache_file, max_age_hours=24):
    if not cache_file.exists():
        return True
    age_seconds = time.time() - cache_file.stat().st_mtime
    return age_seconds > max_age_hours * 3600


def fetch_fred_series(series_id, start_date=None, *, allow_network=True):
    """从 FRED 拉取 CSV；有缓存时先读缓存，仅过期时才短超时刷新。"""
    cache_file = _cache_path(f"fred_{series_id}.csv")
    text = None
    refresh = allow_network and _should_refresh_cache(cache_file)
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8")
        if refresh:
            try:
                response = requests.get(
                    fred_csv_url(series_id),
                    headers=HEADERS,
                    timeout=NDX_FRED_NETWORK_TIMEOUT,
                )
                response.raise_for_status()
                text = response.text
                cache_file.write_text(text, encoding="utf-8")
            except requests.RequestException:
                pass
    elif refresh:
        try:
            response = requests.get(
                fred_csv_url(series_id),
                headers=HEADERS,
                timeout=max(NDX_FRED_NETWORK_TIMEOUT, REQUEST_TIMEOUT),
            )
            response.raise_for_status()
            text = response.text
            cache_file.write_text(text, encoding="utf-8")
        except requests.RequestException as exc:
            raise RuntimeError(f"无法获取 FRED {series_id} 且无本地缓存") from exc
    else:
        raise RuntimeError(f"缺少 FRED {series_id} 本地缓存")

    frame = pd.read_csv(StringIO(text))
    value_col = [column for column in frame.columns if column != "observation_date"][0]
    frame = frame.rename(columns={"observation_date": "date", value_col: "value"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
    frame = frame.dropna(subset=["date", "value"]).sort_values("date")
    if start_date is None:
        start_date = _history_start()
    frame = frame[frame["date"] >= pd.Timestamp(start_date)]
    return frame.reset_index(drop=True)


def fetch_ndx_pe_payload():
    """History of Market：NDX PE（仅保留近 NDX_HISTORY_YEARS 年）。"""
    import json

    cache_file = _cache_path("ndx_forward_pe.json")
    payload = None
    refresh = _should_refresh_cache(cache_file)
    if cache_file.exists() and not refresh:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        try:
            payload = _fetch_json(NDX_FORWARD_PE_URL, timeout=NDX_FRED_NETWORK_TIMEOUT)
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except requests.RequestException:
            if cache_file.exists():
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
            else:
                raise

    current = payload.get("current") or {}
    trailing = pd.DataFrame(payload.get("trailing") or [])
    forward = pd.DataFrame(payload.get("forward") or [])
    if trailing.empty and forward.empty:
        raise RuntimeError("无法获取纳斯达克 100 PE 数据")

    for frame in (trailing, forward):
        if frame.empty:
            continue
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        frame.dropna(subset=["date", "value"], inplace=True)
        frame.sort_values("date", inplace=True)

    trailing = _filter_since(trailing)
    forward = _filter_since(forward)
    return {
        "updated": payload.get("updated"),
        "source": payload.get("source"),
        "current": current,
        "trailing": trailing.reset_index(drop=True),
        "forward": forward.reset_index(drop=True),
    }


def _fetch_us10y_from_akshare():
    cache_file = _cache_path("us10y_akshare.csv")
    if cache_file.exists():
        frame = pd.read_csv(cache_file, parse_dates=["date"])
        return frame

    import akshare as ak

    raw = ak.bond_zh_us_rate()
    out = raw.rename(columns={"日期": "date", "美国国债收益率10年": "us10y"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["us10y"] = pd.to_numeric(out["us10y"], errors="coerce") / 100
    out = out.dropna(subset=["date", "us10y"])
    out = out[["date", "us10y"]].sort_values("date")
    cache_file.write_text(out.to_csv(index=False), encoding="utf-8")
    return out


def fetch_us10y_history():
    """美国 10 年期国债收益率（近 NDX_HISTORY_YEARS 年）。"""
    start = _history_start()
    fred_cache = _cache_path(f"fred_{FRED_US10Y_SERIES}.csv")
    if fred_cache.exists():
        try:
            history = fetch_fred_series(FRED_US10Y_SERIES, start_date=start)
            history["value"] = history["value"] / 100
            return history.rename(columns={"value": "us10y"})
        except (requests.RequestException, RuntimeError):
            pass

    out = _fetch_us10y_from_akshare()
    return out[out["date"] >= start].reset_index(drop=True)


def fetch_ndx_price_history():
    """纳斯达克 100 价格指数（近 NDX_HISTORY_YEARS 年）。"""
    start = _history_start()
    fred_cache = _cache_path(f"fred_{FRED_NASDAQ100_SERIES}.csv")
    if fred_cache.exists():
        try:
            history = fetch_fred_series(
                FRED_NASDAQ100_SERIES, start_date=start, allow_network=True
            )
            return history.rename(columns={"value": "close"})
        except (requests.RequestException, RuntimeError):
            pass

    import akshare as ak

    frame = ak.index_us_stock_sina(symbol=".NDX")
    out = frame.rename(columns={"date": "date", "close": "close"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["date", "close"])
    return out[out["date"] >= start].sort_values("date").reset_index(drop=True)


def fetch_dividend_yield_proxy():
    """以 QQQ 当前股息率近似 NDX（成长指数分红参考价值有限）。"""
    try:
        url = NASDAQ_QQQ_SUMMARY_URL.format(symbol=NDX_DIVIDEND_PROXY_SYMBOL)
        payload = _fetch_json(url, timeout=NDX_FRED_NETWORK_TIMEOUT)
        summary = (payload.get("data") or {}).get("summaryData") or {}
        raw = (summary.get("Yield") or {}).get("value")
        if raw:
            text = str(raw).strip().replace("%", "")
            return float(text) / 100
    except requests.RequestException:
        pass
    return None


def implied_earnings_growth(trailing_pe, forward_pe):
    """由 TTM / Forward PE 隐含未来 12 个月盈利增速。"""
    if trailing_pe is None or forward_pe is None or forward_pe <= 0:
        return None
    return trailing_pe / forward_pe - 1


def build_ndx_valuation_panel():
    """合并 Forward PE（月频）与指数价格，估算历史盈利与增速。"""
    with ThreadPoolExecutor(max_workers=3) as executor:
        pe_future = executor.submit(fetch_ndx_pe_payload)
        price_future = executor.submit(fetch_ndx_price_history)
        us10y_future = executor.submit(fetch_us10y_history)
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


def compute_historical_earnings_growth(panel, years=5):
    """基于隐含盈利（价格/Forward PE）计算多年 CAGR。"""
    if panel is None or len(panel) < years * 6:
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


def attach_percentiles(
    panel,
    window=NDX_PERCENTILE_WINDOW,
    min_days=NDX_PERCENTILE_MIN_DAYS,
):
    """计算 Forward PE、10Y 国债收益率滚动历史分位（默认 10 年窗口）。"""
    if panel is None or panel.empty:
        return None

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


def attach_daily_percentiles(
    panel,
    window=NDX_DAILY_PERCENTILE_WINDOW,
    min_days=NDX_DAILY_PERCENTILE_MIN_DAYS,
):
    """日频面板：Forward PE / TTM PE / 10Y 利率滚动历史分位。"""
    if panel is None or panel.empty:
        return None

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
    """月度 PE 按指数收盘价折算为日度，避免价格上涨而 PE 未更新导致低估。"""
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


def build_ndx_daily_valuation_panel():
    """日频估值面板：价格/10Y 日更，Forward/TTM PE 按月对齐后按收盘价折算。"""
    with ThreadPoolExecutor(max_workers=3) as executor:
        pe_future = executor.submit(fetch_ndx_pe_payload)
        price_future = executor.submit(fetch_ndx_price_history)
        us10y_future = executor.submit(fetch_us10y_history)
        pe_payload = pe_future.result()
        prices = price_future.result()
        us10y = us10y_future.result()

    daily = prices[["date", "close"]].sort_values("date").reset_index(drop=True)

    rates = us10y.sort_values("date")
    daily = pd.merge_asof(daily, rates, on="date", direction="backward")

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
    daily = attach_daily_percentiles(daily)
    daily = attach_pct_above_low(daily, lookback_days=NDX_BUY_LOW_LOOKBACK_DAYS)
    return daily, pe_payload


def fetch_ndx_snapshot(expected_growth=None):
    """拉取纳斯达克 100 最新估值快照（日频分位，PE 按月对齐后按收盘价折算）。"""
    daily, pe_payload = build_ndx_daily_valuation_panel()
    if daily is None or daily.empty:
        raise RuntimeError("无法构建纳斯达克 100 日频估值序列")

    month_panel, _ = build_ndx_valuation_panel()
    hist_growth = compute_historical_earnings_growth(month_panel)

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

    snapshot = {
        "code": NDX_CODE,
        "name": NDX_NAME,
        "date": pd.Timestamp(latest["date"]).date(),
        "trailing_pe": trailing_pe,
        "forward_pe": forward_pe,
        "trailing_pe_percentile": trailing_pe_percentile,
        "forward_pe_percentile": latest.get("forward_pe_percentile"),
        "implied_growth": implied_growth,
        "historical_growth": hist_growth,
        "expected_growth": expected_growth,
        "dividend_yield": fetch_dividend_yield_proxy(),
        "us10y": float(latest["us10y"]) if pd.notna(latest.get("us10y")) else None,
        "us10y_percentile": latest.get("us10y_percentile"),
        "pct_above_low": (
            float(latest["pct_above_low"])
            if pd.notna(latest.get("pct_above_low"))
            else None
        ),
        "history_years": NDX_HISTORY_YEARS,
        "history_days": int(daily["forward_pe"].notna().sum()),
        "trailing_history_days": int(len(trailing_history)),
        "daily_history_days": int(len(daily)),
        "panel": daily,
        "pe_source": pe_payload.get("source"),
    }
    snapshot["expected_growth"] = _resolve_ndx_expected_growth(snapshot)
    return snapshot


def _resolve_ndx_expected_growth(snapshot):
    """避免 ndx_data ↔ ndx_signal 循环依赖，逻辑与 ndx_signal 保持一致。"""
    from ndx_signal import resolve_ndx_expected_growth

    return resolve_ndx_expected_growth(snapshot)
