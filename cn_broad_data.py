"""A 股宽基指数（中证 A500 / 沪深300 / 中证500 / 中证1000 / 科创50 等）估值序列构建。"""

from datetime import date

import pandas as pd

from config import (
    A500_INDEX,
    get_cn_broad_signal_config,
    HS300_INDEX,
    KC50_INDEX,
    ZZ1000_INDEX,
    ZZ500_INDEX,
)
from market_data import (
    asof_datetime,
    attach_bond_yield,
    compute_percentile,
    get_gov_bond_yield_history,
    get_index_perf_history,
    read_indicator_history,
    rolling_percentile_series,
)
from price_position import attach_ma_trend, attach_pct_above_low, attach_pct_below_high, attach_year_range_position, row_price_position_fields
from signal_format import merge_history_meta

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
    index_code, start_date=None, end_date=None, bond_history=None
):
    """构建含 PE、股息率、股债利差的历史序列。"""
    from index_meta import get_index_base_date

    if start_date is None:
        start_date = get_index_base_date(index_code) or "20150101"
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
    panel = attach_bond_yield(panel, bond_history)

    panel["date_dt"] = asof_datetime(panel["date"])
    if indicator is not None and not indicator.empty:
        official = indicator.copy()
        official["date_dt"] = asof_datetime(official["date"])
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
        panel["official_pe"] = pd.to_numeric(panel["official_pe"], errors="coerce")
        panel["rolling_pe"] = pd.to_numeric(panel["rolling_pe"], errors="coerce")
        panel["official_div"] = pd.to_numeric(panel["official_div"], errors="coerce")
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


def attach_cn_broad_percentiles(panel, index_code, window=None, min_days=None):
    """为 PE、股息率、股债利差计算滚动历史分位。"""
    if panel is None or panel.empty:
        return None

    cfg = get_cn_broad_signal_config(index_code)
    window = window if window is not None else cfg["percentile_window"]
    min_days = min_days if min_days is not None else cfg["percentile_min_days"]
    lookback_days = cfg["buy_low_lookback_days"]
    high_lookback_days = cfg.get("buy_high_lookback_days", 252)

    out = panel.copy()
    out["pe_percentile"] = rolling_percentile_series(out["pe"], window, min_days)
    out["dividend_percentile"] = rolling_percentile_series(
        out["dividend_yield"], window, min_days
    )
    out["spread_percentile"] = rolling_percentile_series(
        out["spread"], window, min_days
    )
    out = attach_pct_above_low(out, lookback_days=lookback_days)
    out = attach_pct_below_high(out, lookback_days=high_lookback_days)
    out = attach_year_range_position(
        out, lookback_days=cfg["buy_range_lookback_days"]
    )
    return attach_ma_trend(
        out,
        ma_days=cfg["buy_trend_ma_days"],
        slope_lookback=cfg["buy_trend_slope_lookback_days"],
    )


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
    cfg = get_cn_broad_signal_config(index_code)
    pe = float(latest_row["pe"])
    dividend_yield = float(latest_row["dividend_yield"])
    bond_yield = float(latest_row["bond_yield"])
    spread = float(latest_row["spread"])

    snapshot = {
        "code": meta["code"],
        "name": meta["name"],
        "date": latest_row["date"],
        "close": float(latest_row["close"]),
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
        "year_range_position": (
            float(latest_row["year_range_position"])
            if pd.notna(latest_row.get("year_range_position"))
            else None
        ),
        "ma_slope_pct": (
            float(latest_row["ma_slope_pct"])
            if pd.notna(latest_row.get("ma_slope_pct"))
            else None
        ),
        "below_ma": (
            bool(latest_row["below_ma"])
            if pd.notna(latest_row.get("below_ma"))
            else None
        ),
        "high_lookback_days": cfg.get("buy_high_lookback_days", 252),
        "pb": None,
        "pb_percentile": None,
        "history_days": int(panel["pe"].notna().sum()),
        "panel": panel,
        **row_price_position_fields(latest_row),
    }
    from cn_broad_signal import evaluate_cn_broad_buy
    from sell_trailing import compute_peak_since_last_buy, compute_recent_signal_buy_avg

    def _row_snap(row):
        return {
            "code": index_code,
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "spread_percentile": row.get("spread_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
        }

    lookback = cfg.get("sell_cost_lookback_days", 252)
    snapshot["recent_signal_buy_avg"] = compute_recent_signal_buy_avg(
        panel,
        lambda s: evaluate_cn_broad_buy({**s, "code": index_code}),
        _row_snap,
        lookback_days=lookback,
    )
    snapshot["peak_since_last_buy"] = compute_peak_since_last_buy(
        panel,
        lambda s: evaluate_cn_broad_buy({**s, "code": index_code}),
        _row_snap,
    )
    last_buy_date = None
    for _, row in panel.iterrows():
        if evaluate_cn_broad_buy({**_row_snap(row), "code": index_code})["is_buy"]:
            last_buy_date = pd.Timestamp(row["date"])
    if last_buy_date is not None:
        snapshot["days_since_last_buy"] = (
            pd.Timestamp(latest_row["date"]) - last_buy_date
        ).days
    return merge_history_meta(snapshot, panel)
