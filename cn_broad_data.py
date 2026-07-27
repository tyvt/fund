"""A 股宽基指数（中证 A500 / 沪深300 / 中证500 / 中证1000 / 科创50 等）估值序列构建。"""

from datetime import date

import pandas as pd

from config import (
    A500_INDEX,
    CN_BROAD_PERCENTILE_MIN_DAYS,
    CN_BROAD_PERCENTILE_WINDOW,
    get_cn_broad_signal_config,
    HS300_INDEX,
    KC50_INDEX,
    ZZ1000_INDEX,
    ZZ500_INDEX,
)
from market_data import (
    compute_percentile,
    get_gov_bond_yield_history,
    get_index_perf_history,
    read_indicator_history,
    resolve_bond_yield_for_date,
)
from price_position import attach_pct_above_low, attach_pct_below_high

CN_BROAD_INDEX_BY_CODE = {
    A500_INDEX["code"]: A500_INDEX,
    HS300_INDEX["code"]: HS300_INDEX,
    ZZ500_INDEX["code"]: ZZ500_INDEX,
    ZZ1000_INDEX["code"]: ZZ1000_INDEX,
    KC50_INDEX["code"]: KC50_INDEX,
}


def read_cn_broad_indicator(index_code):
    """读取官方指标文件（近约 20 个交易日）。"""
    return read_indicator_history(index_code)


def calibrate_dividend_ratio(indicator):
    """用官方股息率与 PE 校准估算系数。"""
    if indicator is None or indicator.empty:
        return None
    return float((indicator["dividend_yield"] * indicator["pe"]).mean())


def _rolling_pe_calibration_scale(panel):
    """用官方 PE 与行情 rolling_pe 重叠样本估计校准系数。"""
    overlap = panel.dropna(subset=["official_pe", "rolling_pe"])
    if overlap.empty:
        return 1.0
    return float((overlap["official_pe"] / overlap["rolling_pe"]).median())


def build_cn_broad_valuation_history(
    index_code, start_date="20150101", end_date=None, bond_history=None
):
    """构建含 PE、股息率、股债利差的历史序列。"""
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    if bond_history is None:
        bond_history = get_gov_bond_yield_history()

    indicator = read_cn_broad_indicator(index_code)
    div_pe_ratio = calibrate_dividend_ratio(indicator)

    perf = get_index_perf_history(index_code, start_date, end_date)
    if perf is None or perf.empty:
        return None

    panel = perf.sort_values("date").reset_index(drop=True)
    panel["bond_yield"] = panel["date"].apply(
        lambda d: resolve_bond_yield_for_date(d, bond_history)
    )
    panel = panel.dropna(subset=["bond_yield"])

    panel["date_dt"] = pd.to_datetime(panel["date"])
    if indicator is not None and not indicator.empty:
        official = indicator.copy()
        official["date_dt"] = pd.to_datetime(official["date"])
        official = official.sort_values("date_dt")
        panel = panel.sort_values("date_dt")
        panel = pd.merge_asof(
            panel,
            official[["date_dt", "pe", "dividend_yield"]].rename(
                columns={
                    "pe": "official_pe",
                    "dividend_yield": "official_div",
                }
            ),
            on="date_dt",
            direction="backward",
        )
        scale = _rolling_pe_calibration_scale(panel)
        panel["pe"] = panel["official_pe"].combine_first(
            panel["rolling_pe"] * scale
        )
        if div_pe_ratio is not None:
            panel["dividend_yield"] = panel["official_div"].combine_first(
                div_pe_ratio / panel["pe"]
            )
        else:
            panel["dividend_yield"] = panel["official_div"]
    else:
        panel["pe"] = panel["rolling_pe"]
        if div_pe_ratio is not None:
            panel["dividend_yield"] = div_pe_ratio / panel["pe"]
        else:
            panel["dividend_yield"] = None

    panel = panel.dropna(subset=["pe", "dividend_yield", "bond_yield"])
    panel["spread"] = panel["dividend_yield"] - panel["bond_yield"]
    drop_cols = [
        c
        for c in ("date_dt", "official_pe", "official_div")
        if c in panel.columns
    ]
    if drop_cols:
        panel = panel.drop(columns=drop_cols)
    return panel


def attach_cn_broad_percentiles(
    panel,
    index_code,
    window=CN_BROAD_PERCENTILE_WINDOW,
    min_days=CN_BROAD_PERCENTILE_MIN_DAYS,
):
    """为 PE、股息率、股债利差计算滚动历史分位。"""
    if panel is None or panel.empty:
        return None

    cfg = get_cn_broad_signal_config(index_code)
    lookback_days = cfg["buy_low_lookback_days"]
    high_lookback_days = cfg.get("buy_high_lookback_days", 252)

    out = panel.copy()
    pe_pcts, div_pcts, spread_pcts = [], [], []
    for idx in range(len(out)):
        if idx < min_days:
            pe_pcts.append(None)
            div_pcts.append(None)
            spread_pcts.append(None)
            continue
        start = max(0, idx - window)
        pe_pcts.append(
            compute_percentile(out["pe"].iloc[start:idx], out["pe"].iloc[idx])
        )
        div_pcts.append(
            compute_percentile(
                out["dividend_yield"].iloc[start:idx],
                out["dividend_yield"].iloc[idx],
            )
        )
        spread_pcts.append(
            compute_percentile(
                out["spread"].iloc[start:idx], out["spread"].iloc[idx]
            )
        )

    out["pe_percentile"] = pe_pcts
    out["dividend_percentile"] = div_pcts
    out["spread_percentile"] = spread_pcts
    out = attach_pct_above_low(out, lookback_days=lookback_days)
    return attach_pct_below_high(out, lookback_days=high_lookback_days)


def fetch_cn_broad_snapshot(index_code, bond_history=None):
    """拉取最新指标与历史分位。"""
    meta = CN_BROAD_INDEX_BY_CODE.get(index_code)
    if meta is None:
        raise ValueError(f"未知宽基指数代码: {index_code}")

    if bond_history is None:
        bond_history = get_gov_bond_yield_history()

    panel = build_cn_broad_valuation_history(
        index_code, bond_history=bond_history
    )
    if panel is None or panel.empty:
        raise RuntimeError(f"无法构建 {meta['name']} 估值历史序列")

    panel = attach_cn_broad_percentiles(panel, index_code)
    latest_row = panel.iloc[-1]
    pe = float(latest_row["pe"])
    dividend_yield = float(latest_row["dividend_yield"])
    bond_yield = float(latest_row["bond_yield"])
    spread = float(latest_row["spread"])

    return {
        "code": meta["code"],
        "name": meta["name"],
        "date": latest_row["date"],
        "pe": pe,
        "dividend_yield": dividend_yield,
        "bond_yield": bond_yield,
        "spread": spread,
        "pe_percentile": latest_row["pe_percentile"],
        "dividend_percentile": latest_row["dividend_percentile"],
        "spread_percentile": latest_row["spread_percentile"],
        "pct_above_low": (
            float(latest_row["pct_above_low"])
            if pd.notna(latest_row.get("pct_above_low"))
            else None
        ),
        "pct_below_high": (
            float(latest_row["pct_below_high"])
            if pd.notna(latest_row.get("pct_below_high"))
            else None
        ),
        "pb": None,
        "pb_percentile": None,
        "history_days": int(panel["pe"].notna().sum()),
        "panel": panel,
    }
