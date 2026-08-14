"""数据源交叉校验：国债补全后抽检、ETF 跟踪、美股 Forward PE 多源比对。"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import date, timedelta
from io import StringIO

import akshare as ak
import pandas as pd
import requests

from config import HEADERS, REQUEST_TIMEOUT
from data_cache import get_or_fetch_dataframe, get_or_fetch_us_json
from data_sources import (
    BARRONS_PE_YIELDS_URL,
    MULTPL_SP500_PE_TABLE_URL,
    YARDENI_FORWARD_PE_CHART_URL,
)
from market_data import get_gov_bond_yield_history, get_index_perf_history

BARRONS_INDEX_LABELS = {
    "spx": "S&P 500 Index",
    "ndx": "NASDAQ 100 Index",
}
BARRONS_FETCH_TIMEOUT = 60

# H30269 无第三方指数 K 线，用跟踪红利/低波 ETF 做日收益相关性抽检
DIVIDEND_INDEX_ETF_PROXIES = {
    "H30269": [
        ("560150", "sh560150", "红利低波ETF（跟踪中证红利低波相关指数，近期相关性最高）"),
        ("512890", "sh512890", "华泰柏瑞中证红利ETF（跟踪中证红利，非 H30269 本指数）"),
        ("515450", "sh515450", "南方红利低波50ETF（低波红利近似）"),
    ],
}


def bond_history_coverage(panel_dates: pd.Series, bond_history=None) -> dict:
    """统计面板日期中有多少天使用日度国债、多少天使用年度回填。"""
    bond = bond_history if bond_history is not None else get_gov_bond_yield_history()
    if bond is None or bond.empty:
        return {"daily_days": 0, "fallback_days": len(panel_dates), "fallback_pct": 100.0}
    bond_dates = set(pd.to_datetime(bond["date"]).dt.date)
    panel_days = pd.to_datetime(panel_dates).dt.date
    daily = sum(1 for d in panel_days if d in bond_dates)
    total = len(panel_days)
    fallback = total - daily
    return {
        "daily_days": daily,
        "fallback_days": fallback,
        "fallback_pct": round(fallback / total * 100, 1) if total else 0.0,
        "bond_start": str(pd.to_datetime(bond["date"]).min().date()),
        "bond_end": str(pd.to_datetime(bond["date"]).max().date()),
        "bond_rows": len(bond),
    }


def _fetch_etf_close_sina(symbol: str, start: str, end: str) -> pd.DataFrame:
    df = ak.fund_etf_hist_sina(symbol=symbol)
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "close"])
    out = df.rename(columns={"date": "date", "close": "close"}).copy()
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["date", "close"])
    mask = (out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))
    return out.loc[mask].sort_values("date").reset_index(drop=True)


def check_index_etf_return_correlation(
    index_code: str,
    *,
    years: int = 5,
    etf_proxies: list[tuple[str, str, str]] | None = None,
) -> list[dict]:
    """指数日收益与跟踪 ETF 日收益的 Pearson 相关系数。"""
    end = date.today()
    start = (end - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    end_s = end.strftime("%Y-%m-%d")

    idx = get_index_perf_history(index_code, start_date=start.replace("-", ""), end_date=end_s.replace("-", ""))
    if idx is None or idx.empty:
        return [{"index": index_code, "status": "指数行情缺失"}]

    idx = idx[["date", "close"]].copy()
    idx["date"] = pd.to_datetime(idx["date"])
    idx["ret"] = idx["close"].pct_change()

    proxies = etf_proxies or DIVIDEND_INDEX_ETF_PROXIES.get(index_code, [])
    results = []
    for etf_code, sina_symbol, note in proxies:
        etf = _fetch_etf_close_sina(sina_symbol, start, end_s)
        if etf.empty:
            results.append(
                {
                    "index": index_code,
                    "etf": etf_code,
                    "note": note,
                    "status": "ETF 数据缺失",
                }
            )
            continue
        etf["ret"] = etf["close"].pct_change()
        merged = idx.merge(etf[["date", "ret"]], on="date", how="inner", suffixes=("_idx", "_etf"))
        merged = merged.dropna(subset=["ret_idx", "ret_etf"])
        if len(merged) < 60:
            results.append(
                {
                    "index": index_code,
                    "etf": etf_code,
                    "note": note,
                    "overlap_days": len(merged),
                    "status": "样本不足",
                }
            )
            continue
        corr = float(merged["ret_idx"].corr(merged["ret_etf"]))
        status = "高度相关" if corr >= 0.95 else "中度相关" if corr >= 0.85 else "弱相关"
        results.append(
            {
                "index": index_code,
                "etf": etf_code,
                "note": note,
                "overlap_days": len(merged),
                "return_corr": round(corr, 4),
                "status": status,
            }
        )
    return results


def _flatten_column_name(col) -> str:
    if isinstance(col, tuple):
        return " ".join(str(part) for part in col if str(part) not in ("nan", "None"))
    return str(col)


def _fetch_html_via_curl(url: str, *, timeout: int = 60) -> str:
    """Barron's / WSJ 在部分 Windows Python 环境下 requests SSL 握手失败，改用 curl。"""
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("需要系统 curl 抓取 Barron's（本机 Python requests 与 WSJ SSL 不兼容）")
    result = subprocess.run(
        [curl, "-sS", "-L", "-A", "Mozilla/5.0", "--max-time", str(timeout), url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"curl 退出码 {result.returncode}")
    if not result.stdout.strip():
        raise RuntimeError("Barron's 返回空页面（请确认 VPN 已开启）")
    return result.stdout


def _parse_barrons_pe_tables(tables: list[pd.DataFrame]) -> tuple[dict[str, dict], str | None]:
    indices: dict[str, dict] = {}
    as_of: str | None = None
    for table in tables:
        cols = list(table.columns)
        forward_col = next(
            (col for col in cols if "estimate" in _flatten_column_name(col).lower()),
            None,
        )
        if forward_col is None:
            continue
        trailing_col = next(
            (
                col
                for col in cols
                if "p/e" in _flatten_column_name(col).lower()
                and "estimate" not in _flatten_column_name(col).lower()
                and "year ago" not in _flatten_column_name(col).lower()
            ),
            None,
        )
        if trailing_col is not None and as_of is None:
            match = re.search(r"(\d{1,2}/\d{1,2}/\d{2})", _flatten_column_name(trailing_col))
            if match:
                as_of = match.group(1)
        name_col = cols[0]
        for _, row in table.iterrows():
            label = str(row[name_col]).strip()
            for key, expected in BARRONS_INDEX_LABELS.items():
                if key in indices or expected.lower() not in label.lower():
                    continue
                forward_pe = pd.to_numeric(row.get(forward_col), errors="coerce")
                trailing_pe = (
                    pd.to_numeric(row.get(trailing_col), errors="coerce")
                    if trailing_col is not None
                    else pd.NA
                )
                if pd.isna(forward_pe):
                    continue
                indices[key] = {
                    "forward_pe": float(forward_pe),
                    "trailing_pe": float(trailing_pe) if pd.notna(trailing_pe) else None,
                }
    return indices, as_of


def fetch_barrons_forward_pe_snapshot() -> dict:
    """Barron's Birinyi Forward PE 最新快照（SPX / NDX，周频）。"""

    def _download() -> dict:
        html = _fetch_html_via_curl(BARRONS_PE_YIELDS_URL, timeout=BARRONS_FETCH_TIMEOUT)
        tables = pd.read_html(StringIO(html))
        indices, as_of = _parse_barrons_pe_tables(tables)
        if not indices:
            raise RuntimeError("Barron's 页面未解析到 S&P / NASDAQ Forward PE 表格")
        return {
            "source": "barrons_birinyi",
            "url": BARRONS_PE_YIELDS_URL,
            "as_of": as_of,
            "indices": indices,
        }

    return get_or_fetch_us_json("barrons_forward_pe_snapshot.json", _download)


def _barrons_hom_forward_check(key: str, hom_forward_pe: float | None) -> dict:
    snapshot = fetch_barrons_forward_pe_snapshot()
    row = (snapshot.get("indices") or {}).get(key) or {}
    barrons_forward = row.get("forward_pe")
    out = {
        "barrons_forward_pe": barrons_forward,
        "barrons_trailing_pe": row.get("trailing_pe"),
        "barrons_as_of": snapshot.get("as_of"),
    }
    if barrons_forward is None or hom_forward_pe is None or hom_forward_pe <= 0:
        out["barrons_status"] = "Barron's 或 HOM Forward PE 缺失"
        return out
    diff = barrons_forward - hom_forward_pe
    diff_pct = abs(diff) / hom_forward_pe * 100
    out["barrons_vs_hom_forward_diff"] = round(diff, 2)
    out["barrons_vs_hom_forward_diff_pct"] = round(diff_pct, 1)
    if diff_pct <= 8.0:
        out["barrons_status"] = "与 Barron's 同量级（口径/日期差可接受）"
    elif diff_pct <= 15.0:
        out["barrons_status"] = "与 Barron's 存在口径偏差"
    else:
        out["barrons_status"] = "与 Barron's 偏差较大，宜人工核对"
    return out


def _compare_hom_multpl_trailing(
    hom_trailing: pd.DataFrame,
    *,
    months: int,
) -> dict:
    multpl = fetch_multpl_sp500_trailing_pe()
    hom_trailing = hom_trailing.copy()
    hom_trailing["month"] = hom_trailing["date"].dt.to_period("M")
    multpl["month"] = multpl["date"].dt.to_period("M")

    cutoff = pd.Timestamp(date.today()) - pd.DateOffset(months=months)
    trail_m = (
        hom_trailing[hom_trailing["date"] >= cutoff]
        .groupby("month", as_index=False)
        .last()[["month", "value"]]
        .rename(columns={"value": "hom_trailing_pe"})
    )
    multpl_m = multpl[multpl["date"] >= cutoff][["month", "trailing_pe"]].rename(
        columns={"trailing_pe": "multpl_trailing_pe"}
    )
    merged = trail_m.merge(multpl_m, on="month", how="inner")
    if merged.empty:
        return {"multpl_status": "无重叠月份"}

    merged["hom_vs_multpl_trail_diff"] = merged["hom_trailing_pe"] - merged["multpl_trailing_pe"]
    latest = merged.iloc[-1]
    max_abs = float(merged["hom_vs_multpl_trail_diff"].abs().max())
    return {
        "months": len(merged),
        "latest_month": str(latest["month"]),
        "latest_hom_trailing_pe": float(latest["hom_trailing_pe"]),
        "latest_multpl_trailing_pe": float(latest["multpl_trailing_pe"]),
        "hom_trailing_vs_multpl_mean_diff": float(merged["hom_vs_multpl_trail_diff"].mean()),
        "hom_trailing_vs_multpl_max_abs_diff": max_abs,
        "multpl_status": "可靠" if max_abs < 3.0 else "Trailing PE 与 Multpl 偏差较大",
    }


def fetch_multpl_sp500_trailing_pe() -> pd.DataFrame:
    """Multpl.com 标普 500 滚动市盈率（月频，Trailing PE）。"""
    def _download():
        response = requests.get(
            MULTPL_SP500_PE_TABLE_URL,
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
        if not tables:
            raise RuntimeError("Multpl 页面无表格")
        out = tables[0].rename(columns={"Date": "date", "Value": "trailing_pe"})
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["trailing_pe"] = pd.to_numeric(out["trailing_pe"], errors="coerce")
        return out.dropna(subset=["date", "trailing_pe"]).sort_values("date").reset_index(drop=True)

    return get_or_fetch_dataframe("multpl_sp500_pe", _download, subdir="us")


def compare_us_forward_pe_sources(key: str = "spx", *, months: int = 120) -> dict:
    """HOM Forward PE 与 Barron's Birinyi / Multpl Trailing 交叉比对。"""
    from us_index_data import fetch_pe_payload

    if key not in BARRONS_INDEX_LABELS:
        return {"key": key, "status": f"未知指数 {key}"}

    payload = fetch_pe_payload(key)
    hom_forward = payload["forward"].copy()
    hom_trailing = payload["trailing"].copy()

    latest_fwd_row = hom_forward.iloc[-1] if not hom_forward.empty else None
    latest_trail_row = hom_trailing.iloc[-1] if not hom_trailing.empty else None
    hom_forward_pe = (
        float(latest_fwd_row["value"]) if latest_fwd_row is not None else None
    )
    hom_trailing_pe = (
        float(latest_trail_row["value"]) if latest_trail_row is not None else None
    )
    hom_forward_date = (
        latest_fwd_row["date"].date() if latest_fwd_row is not None else None
    )
    hom_forward_vs_trailing = (
        hom_forward_pe - hom_trailing_pe
        if hom_forward_pe is not None and hom_trailing_pe is not None
        else None
    )

    result: dict = {
        "key": key,
        "latest_hom_forward_pe": hom_forward_pe,
        "latest_hom_trailing_pe": hom_trailing_pe,
        "latest_hom_forward_date": str(hom_forward_date) if hom_forward_date else None,
        "hom_forward_vs_trailing_latest": hom_forward_vs_trailing,
        "yardeni_note": (
            f"Yardeni Forward P/E 无公开下载，请人工对照图表：{YARDENI_FORWARD_PE_CHART_URL}"
        ),
    }
    result.update(_barrons_hom_forward_check(key, hom_forward_pe))

    if key == "spx":
        result.update(_compare_hom_multpl_trailing(hom_trailing, months=months))
        statuses = [result.get("barrons_status"), result.get("multpl_status")]
    else:
        statuses = [result.get("barrons_status")]

    statuses = [s for s in statuses if s]
    if not statuses:
        result["status"] = "校验数据缺失"
    elif any("偏差较大" in s or "缺失" in s for s in statuses):
        result["status"] = "；".join(statuses)
    elif any("口径偏差" in s for s in statuses):
        result["status"] = "；".join(statuses)
    else:
        result["status"] = "；".join(statuses)
    return result


def check_cyb_pe_against_szse_index() -> dict:
    """深交所创业板 PE 与乐咕「深证」市场 PE 的近期偏差（口径不同，仅作 sanity check）。"""
    from cyb_data import fetch_cyb_pe_szse_official

    cyb = fetch_cyb_pe_szse_official()
    try:
        sz = ak.stock_market_pe_lg(symbol="深证")
        sz = sz.rename(columns={"日期": "date", "平均市盈率": "pe_sz"})
        sz["date"] = pd.to_datetime(sz["date"])
    except Exception as exc:
        return {"status": f"深证 PE 拉取失败: {exc}"}

    merged = cyb.merge(sz[["date", "pe_sz"]], on="date", how="inner", suffixes=("_cyb", ""))
    if merged.empty:
        return {"status": "无重叠日期"}
    merged["diff_pct"] = (merged["pe"] - merged["pe_sz"]).abs() / merged["pe_sz"]
    return {
        "overlap_months": len(merged),
        "mean_diff_pct": float(merged["diff_pct"].mean() * 100),
        "max_diff_pct": float(merged["diff_pct"].max() * 100),
        "note": "创业板 PE 与深证全市场 PE 口径不同，偏差大属正常",
        "status": "已对齐深交所创业板 PE",
    }
