"""红利指数数据拉取与信号历史构建。"""

from datetime import date

import pandas as pd

from config import (
    DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
    DIVIDEND_SPREAD_PERCENTILE_WINDOW,
    DIVIDEND_SIGNAL_HISTORY_START,
    get_dividend_signal_config,
)
from market_data import (
    compute_percentile,
    get_gov_bond_yield_history,
    get_index_perf_history,
    read_indicator_history,
    resolve_bond_yield_for_date,
)
from price_position import attach_pct_above_low, price_position_ok


def get_index_data(index_code):
    """获取最新 PE、股息率与数据日期。"""
    indicator = read_indicator_history(index_code)
    if indicator is None or indicator.empty:
        return None, None, None
    latest = indicator.iloc[-1]
    return float(latest["pe"]), float(latest["dividend_yield"]), latest["date"]


def calibrate_dividend_ratio(indicator):
    """用官方股息率与 PE 校准估算系数。"""
    if indicator is None or indicator.empty:
        return None
    return float((indicator["dividend_yield"] * indicator["pe"]).mean())


def build_signal_history(
    index_code, start_date=None, end_date=None, bond_history=None
):
    """构建含 PE、股息率、利差及滚动分位的历史序列。

    PE 优先用官方指标（merge_asof 对齐），缺失时用行情 API 的滚动 PE；
    股息率优先用官方值，缺失时用官方校准系数 / PE 估算。
    """
    if start_date is None:
        start_date = DIVIDEND_SIGNAL_HISTORY_START
    if end_date is None:
        end_date = date.today().strftime("%Y%m%d")
    if bond_history is None:
        bond_history = get_gov_bond_yield_history()

    indicator = read_indicator_history(index_code)
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
        panel["pe"] = panel["official_pe"].combine_first(panel["rolling_pe"])
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

    pe_pcts, spread_pcts = [], []
    for idx in range(len(panel)):
        if idx < DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS:
            pe_pcts.append(None)
            spread_pcts.append(None)
            continue
        start = max(0, idx - DIVIDEND_SPREAD_PERCENTILE_WINDOW)
        pe_pcts.append(compute_percentile(panel["pe"].iloc[start:idx], panel["pe"].iloc[idx]))
        spread_pcts.append(
            compute_percentile(panel["spread"].iloc[start:idx], panel["spread"].iloc[idx])
        )

    panel["pe_percentile"] = pe_pcts
    panel["spread_percentile"] = spread_pcts
    drop_cols = [c for c in ("date_dt", "official_pe", "official_div") if c in panel.columns]
    if drop_cols:
        panel = panel.drop(columns=drop_cols)

    cfg = get_dividend_signal_config(index_code)
    panel = attach_pct_above_low(
        panel,
        lookback_days=cfg.get("buy_low_lookback_days", 60),
    )
    return panel


def is_buy_signal(
    spread,
    spread_percentile,
    pe_percentile,
    index_code,
    pct_above_low=None,
):
    """买入条件须全部满足（阈值按指数读取）。"""
    if spread is None or spread_percentile is None or pe_percentile is None:
        return False
    cfg = get_dividend_signal_config(index_code)
    base = (
        spread > cfg["buy_spread_min"]
        and spread_percentile >= cfg["buy_spread_percentile_min"]
        and pe_percentile <= cfg["buy_pe_percentile_max"]
    )
    max_above_low = cfg.get("buy_max_above_low_pct")
    if not price_position_ok(pct_above_low, max_above_low):
        return False
    return base


def is_buy_signal_row(row, index_code):
    """基于估值面板行判断是否买入（与回测/报告共用）。"""
    return is_buy_signal(
        row.get("spread"),
        row.get("spread_percentile"),
        row.get("pe_percentile"),
        index_code,
        pct_above_low=row.get("pct_above_low"),
    )


def evaluate_buy_signal(index_code, pe, dividend_yield, bond_yield, bond_history=None):
    """评估当前是否满足买入条件（与回测共用滚动分位及历史面板末行）。"""
    if bond_history is None:
        bond_history = get_gov_bond_yield_history()

    panel = build_signal_history(index_code, bond_history=bond_history)
    if panel is None or panel.empty:
        return {
            "pe": None,
            "dividend_yield": None,
            "index_date": None,
            "spread": None,
            "spread_percentile": None,
            "pe_percentile": None,
            "is_buy": False,
            "panel": None,
        }

    latest = panel.iloc[-1]
    spread = latest["spread"]
    spread_percentile = latest["spread_percentile"]
    pe_percentile = latest["pe_percentile"]
    pct_above_low = latest.get("pct_above_low")

    return {
        "pe": float(latest["pe"]),
        "dividend_yield": float(latest["dividend_yield"]),
        "index_date": latest["date"],
        "spread": spread,
        "spread_percentile": spread_percentile,
        "pe_percentile": pe_percentile,
        "pct_above_low": (
            float(pct_above_low) if pct_above_low is not None and not pd.isna(pct_above_low) else None
        ),
        "is_buy": is_buy_signal_row(latest, index_code),
        "panel": panel,
    }


def collect_index_results(indices, bond_history, bond_yield):
    """拉取指定指数列表的行情。"""
    index_results = []
    for index in indices:
        print(f"正在获取 {index['code']} {index['name']} ...")
        pe, dividend_yield, index_date = get_index_data(index["code"])
        index_results.append(
            {
                "code": index["code"],
                "name": index["name"],
                "pe": pe,
                "dividend_yield": dividend_yield,
                "index_date": index_date,
            }
        )
    return index_results
