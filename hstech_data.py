"""恒生科技指数（HSTECH）估值数据拉取与历史分位计算。"""

from datetime import datetime
from hashlib import md5

import pandas as pd
from bs4 import BeautifulSoup
from curl_cffi import requests as http

from config import (
    BUY_RANGE_LOOKBACK_DAYS,
    BUY_TREND_MA_DAYS,
    BUY_TREND_SLOPE_LOOKBACK_DAYS,
    HSTECH_BUY_HIGH_LOOKBACK_DAYS,
    HSTECH_BUY_LOW_LOOKBACK_DAYS,
    HSTECH_DIV_PERCENTILE_WINDOW,
    HSTECH_INDEX,
    HSTECH_PERCENTILE_MIN_DAYS,
    HSTECH_PERCENTILE_WINDOW,
)
from data_cache import get_or_fetch_dataframe
from market_data import compute_percentile
from price_position import (
    attach_ma_trend,
    attach_pct_above_low,
    attach_pct_below_high,
    attach_year_range_position,
    row_price_position_fields,
)
HSTECH_CODE = HSTECH_INDEX["code"]
HSTECH_NAME = HSTECH_INDEX["name"]
HSTECH_TENCENT_SYMBOL = "hkHSTECH"
LEGULEGU_PAGE = (
    "https://www.legulegu.com/stockdata/hsi-theme-index?indexCode=HSTECH"
)


def _legulegu_token():
    return md5(datetime.now().date().isoformat().encode()).hexdigest()


def _legulegu_session():
    response = http.get(LEGULEGU_PAGE, impersonate="chrome", timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    csrf_tag = soup.find("meta", attrs={"name": "_csrf"})
    if csrf_tag is None:
        raise RuntimeError("无法读取乐咕乐股 CSRF Token")
    return response.cookies, csrf_tag.attrs["content"]


def _download_hstech_pe_dividend():
    cookies, csrf = _legulegu_session()
    url = (
        "https://www.legulegu.com/api/stockdata/hsidata"
        f"?indexCode={HSTECH_CODE}&token={_legulegu_token()}"
    )
    response = http.get(
        url,
        cookies=cookies,
        headers={"Referer": LEGULEGU_PAGE, "X-CSRF-Token": csrf},
        impersonate="chrome",
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError("乐咕乐股未返回恒生科技估值历史")

    out = pd.DataFrame(rows).rename(
        columns={"date": "date", "pe": "pe", "dv": "dividend_yield"}
    )
    out["date"] = pd.to_datetime(out["date"])
    out["pe"] = pd.to_numeric(out["pe"], errors="coerce")
    out["dividend_yield"] = pd.to_numeric(out["dividend_yield"], errors="coerce") / 100
    return out.dropna(subset=["date", "pe"]).sort_values("date").reset_index(drop=True)


def fetch_hstech_pe_dividend_history():
    """乐咕乐股恒生科技指数市盈率与股息率（月度）。"""
    return get_or_fetch_dataframe(
        "hstech_pe_dividend",
        _download_hstech_pe_dividend,
        subdir="hstech",
    )


def _download_hstech_price():
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
        f"?param={HSTECH_TENCENT_SYMBOL},day,,,2000"
    )
    response = http.get(url, impersonate="chrome", timeout=20)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("无法获取恒生科技指数日线行情")
    series = (
        data.get(HSTECH_TENCENT_SYMBOL, {}).get("day")
        or data.get(HSTECH_TENCENT_SYMBOL, {}).get("qfqday")
        or []
    )
    if not series:
        raise RuntimeError("无法获取恒生科技指数日线行情")

    out = pd.DataFrame(series, columns=["date", "open", "close", "high", "low", "volume"])
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    return out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def fetch_hstech_price_history():
    """恒生科技指数日线收盘价（腾讯财经）。"""
    return get_or_fetch_dataframe(
        "hstech_price",
        _download_hstech_price,
        subdir="hstech",
    )


def build_hstech_valuation_panel():
    """合并月度 PE、股息率为日度估值面板（PE 按指数价日度折算）。"""
    pe = fetch_hstech_pe_dividend_history()
    prices = fetch_hstech_price_history()

    panel = prices[["date", "close"]].sort_values("date").copy()
    pe_src = pe.sort_values("date").rename(
        columns={"date": "pe_source_date", "pe": "pe_official", "dividend_yield": "dividend_yield"}
    )
    panel = pd.merge_asof(
        panel,
        pe_src[["pe_source_date", "pe_official", "dividend_yield"]],
        left_on="date",
        right_on="pe_source_date",
        direction="backward",
    )
    anchor = prices.rename(
        columns={"date": "pe_source_date", "close": "close_at_pe_source"}
    )
    panel = panel.merge(
        anchor[["pe_source_date", "close_at_pe_source"]],
        on="pe_source_date",
        how="left",
    )
    panel["pe"] = panel["pe_official"] * (
        panel["close"] / panel["close_at_pe_source"]
    )
    panel = panel.dropna(subset=["pe", "dividend_yield"])
    panel = panel[panel["pe"] > 0]
    panel["date_only"] = panel["date"].dt.date
    drop_cols = ["pe_source_date", "close_at_pe_source", "pe_official", "close"]
    panel = panel.drop(columns=[c for c in drop_cols if c in panel.columns])
    return panel.reset_index(drop=True)


def compute_annualized_volatility(price_history, window=252):
    if price_history is None or price_history.empty:
        return None
    prices = price_history.sort_values("date").copy()
    prices["ret"] = prices["close"].pct_change()
    recent = prices["ret"].dropna().tail(window)
    if len(recent) < window // 2:
        return None
    return float(recent.std() * (252**0.5))


def attach_percentiles(
    panel,
    window=HSTECH_PERCENTILE_WINDOW,
    div_window=HSTECH_DIV_PERCENTILE_WINDOW,
    min_days=HSTECH_PERCENTILE_MIN_DAYS,
):
    if panel is None or panel.empty:
        return None

    out = panel.copy()
    pe_pcts, div_pcts = [], []

    for idx in range(len(out)):
        if idx < min_days:
            pe_pcts.append(None)
            div_pcts.append(None)
            continue

        pe_start = max(0, idx - window)
        div_start = max(0, idx - div_window)
        pe_pcts.append(
            compute_percentile(out["pe"].iloc[pe_start:idx], out["pe"].iloc[idx])
        )
        div_pcts.append(
            compute_percentile(
                out["dividend_yield"].iloc[div_start:idx],
                out["dividend_yield"].iloc[idx],
            )
        )

    out["pe_percentile"] = pe_pcts
    out["dividend_percentile"] = div_pcts
    return out


def fetch_hstech_snapshot(expected_growth=None):
    panel = build_hstech_valuation_panel()
    if panel is None or panel.empty:
        raise RuntimeError("无法构建恒生科技指数估值历史序列")

    panel = attach_percentiles(panel)
    price_history = fetch_hstech_price_history()
    price_history["date_only"] = pd.to_datetime(price_history["date"]).dt.date
    panel = panel.merge(
        price_history[["date_only", "close"]],
        on="date_only",
        how="left",
    )
    panel = attach_pct_above_low(panel, lookback_days=HSTECH_BUY_LOW_LOOKBACK_DAYS)
    panel = attach_pct_below_high(
        panel, lookback_days=HSTECH_BUY_HIGH_LOOKBACK_DAYS
    )
    panel = attach_year_range_position(
        panel, lookback_days=BUY_RANGE_LOOKBACK_DAYS, date_col="date_only"
    )
    panel = attach_ma_trend(
        panel,
        ma_days=BUY_TREND_MA_DAYS,
        slope_lookback=BUY_TREND_SLOPE_LOOKBACK_DAYS,
    )
    latest = panel.iloc[-1]
    volatility = compute_annualized_volatility(price_history)
    pct_above_low = (
        float(latest["pct_above_low"])
        if pd.notna(latest.get("pct_above_low"))
        else None
    )
    pct_below_high = (
        float(latest["pct_below_high"])
        if pd.notna(latest.get("pct_below_high"))
        else None
    )
    year_range_position = (
        float(latest["year_range_position"])
        if pd.notna(latest.get("year_range_position"))
        else None
    )

    return {
        "code": HSTECH_CODE,
        "name": HSTECH_NAME,
        "date": latest["date_only"],
        "pe": float(latest["pe"]),
        "dividend_yield": float(latest["dividend_yield"]),
        "pe_percentile": latest["pe_percentile"],
        "dividend_percentile": latest["dividend_percentile"],
        "pct_above_low": pct_above_low,
        "pct_below_high": pct_below_high,
        "year_range_position": year_range_position,
        "ma_slope_pct": (
            float(latest["ma_slope_pct"])
            if pd.notna(latest.get("ma_slope_pct"))
            else None
        ),
        "volatility": volatility,
        "history_days": int(panel["pe"].notna().sum()),
        "panel": panel,
        "expected_growth": expected_growth,
        "high_lookback_days": HSTECH_BUY_HIGH_LOOKBACK_DAYS,
        **row_price_position_fields(latest),
    }
