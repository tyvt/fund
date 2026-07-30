"""红利指数数据拉取与信号历史构建。"""

from datetime import date

import pandas as pd

from config import (
    BUY_RANGE_LOOKBACK_DAYS,
    DIVIDEND_SPREAD_10Y_MIN_DAYS,
    DIVIDEND_SPREAD_10Y_WINDOW,
    DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN,
    DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
    DIVIDEND_SPREAD_PERCENTILE_WINDOW,
    DIVIDEND_SIGNAL_HISTORY_START,
    get_dividend_signal_config,
)
from market_data import (
    attach_bond_yield,
    compute_percentile,
    get_gov_bond_yield_history,
    get_index_perf_history,
    read_indicator_history,
    rolling_percentile_series,
    rolling_window_stats,
)
from price_position import (
    attach_pct_above_low,
    attach_pct_below_high,
    attach_year_range_position,
    drawdown_from_high_ok,
    effective_drawdown_threshold,
    effective_max_above_low_pct,
    is_near_year_low,
    price_position_ok,
    row_price_position_fields,
    year_range_ok,
)
from signal_format import panel_history_meta


def spread_10y_window_stats(spread_series, idx):
    """计算近10年（或可用样本）利差区间与分位。"""
    if idx < DIVIDEND_SPREAD_10Y_MIN_DAYS:
        return None, None, None, None
    start = max(0, idx - DIVIDEND_SPREAD_10Y_WINDOW)
    hist = spread_series.iloc[start:idx]
    current = spread_series.iloc[idx]
    if hist.empty or current is None or pd.isna(current):
        return None, None, None, None
    pct = compute_percentile(hist, current)
    return (
        float(hist.min()),
        float(hist.max()),
        pct,
        int(len(hist)),
    )


def assess_spread_10y_level(spread_10y_pct, high_pct_min=None):
    """判断近10年利差分位是否偏高。"""
    if spread_10y_pct is None or pd.isna(spread_10y_pct):
        return None, "样本不足"
    threshold = high_pct_min if high_pct_min is not None else DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN
    if spread_10y_pct >= threshold:
        return False, "利差偏高"
    return True, "未过高"


def format_dividend_spread_10y_line(
    spread,
    spread_10y_pct,
    spread_10y_min,
    spread_10y_max,
    sample_days,
    high_pct_min=None,
):
    """报告用近10年利差区间与偏高判定行。"""
    threshold = (
        high_pct_min if high_pct_min is not None else DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN
    )
    if sample_days is None or sample_days < DIVIDEND_SPREAD_10Y_MIN_DAYS:
        return "近10年利差: 历史样本不足"
    years = sample_days / 252
    period_label = "近10年" if years >= 9 else f"近{max(1, int(round(years)))}年"
    range_text = "—"
    if spread_10y_min is not None and spread_10y_max is not None:
        range_text = f"{spread_10y_min:.2%}–{spread_10y_max:.2%}"
    spread_text = f"{spread:.2%}" if spread is not None else "—"
    pct_text = (
        f"{spread_10y_pct:.1f}%"
        if spread_10y_pct is not None and not pd.isna(spread_10y_pct)
        else "—"
    )
    _, verdict = assess_spread_10y_level(spread_10y_pct, threshold)
    return (
        f"{period_label}利差 {range_text} | 当前 {spread_text} | "
        f"{period_label}分位 {pct_text}（偏高线≥{threshold:.0f}%）| 判定: {verdict}"
    )


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
        from index_meta import get_index_base_date

        start_date = get_index_base_date(index_code) or DIVIDEND_SIGNAL_HISTORY_START
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
    panel = attach_bond_yield(panel, bond_history)

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

    panel["pe_percentile"] = rolling_percentile_series(
        panel["pe"],
        DIVIDEND_SPREAD_PERCENTILE_WINDOW,
        DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
    )
    panel["spread_percentile"] = rolling_percentile_series(
        panel["spread"],
        DIVIDEND_SPREAD_PERCENTILE_WINDOW,
        DIVIDEND_SPREAD_PERCENTILE_MIN_DAYS,
    )
    spread_10y_mins, spread_10y_maxs, spread_10y_pcts, spread_10y_samples = (
        rolling_window_stats(
            panel["spread"],
            DIVIDEND_SPREAD_10Y_WINDOW,
            DIVIDEND_SPREAD_10Y_MIN_DAYS,
        )
    )
    panel["spread_10y_min"] = spread_10y_mins
    panel["spread_10y_max"] = spread_10y_maxs
    panel["spread_10y_percentile"] = spread_10y_pcts
    panel["spread_10y_sample_days"] = spread_10y_samples
    drop_cols = [c for c in ("date_dt", "official_pe", "official_div") if c in panel.columns]
    if drop_cols:
        panel = panel.drop(columns=drop_cols)

    cfg = get_dividend_signal_config(index_code)
    panel = attach_pct_above_low(
        panel,
        lookback_days=cfg.get("buy_low_lookback_days", 60),
    )
    panel = attach_pct_below_high(
        panel,
        lookback_days=cfg.get("buy_high_lookback_days", 252),
    )
    panel = attach_year_range_position(panel, lookback_days=BUY_RANGE_LOOKBACK_DAYS)
    panel = attach_total_return_close(panel, index_code)
    return panel


def attach_total_return_close(panel, index_code):
    """合并全收益指数收盘价，用于回测收益率（分红再投资）。"""
    from config import get_dividend_total_return_code

    if panel is None or panel.empty:
        return panel
    tr_code = get_dividend_total_return_code(index_code)
    if not tr_code:
        panel = panel.copy()
        panel["total_return_close"] = panel["close"]
        return panel

    start = panel["date"].min()
    end = panel["date"].max()
    if hasattr(start, "strftime"):
        start_s = start.strftime("%Y%m%d")
        end_s = end.strftime("%Y%m%d")
    else:
        start_s = str(start).replace("-", "")
        end_s = str(end).replace("-", "")

    tr_hist = get_index_perf_history(tr_code, start_s, end_s)
    if tr_hist is None or tr_hist.empty:
        panel = panel.copy()
        panel["total_return_close"] = panel["close"]
        return panel

    tr = tr_hist[["date", "close"]].rename(columns={"close": "total_return_close"})
    out = panel.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    tr["date"] = pd.to_datetime(tr["date"]).dt.date
    out = out.merge(tr, on="date", how="left")
    out["total_return_close"] = out["total_return_close"].fillna(out["close"])
    return out


def is_buy_signal(
    spread,
    spread_percentile,
    pe_percentile,
    index_code,
    pct_above_low=None,
    pct_below_high=None,
    year_range_position=None,
):
    """买入条件须全部满足（阈值按指数读取）。"""
    if spread is None:
        return False
    cfg = get_dividend_signal_config(index_code)
    near_low = is_near_year_low(
        year_range_position, cfg.get("buy_near_year_low_range_pct")
    )
    extreme_low = (
        year_range_position is not None
        and not pd.isna(year_range_position)
        and year_range_position <= cfg.get("buy_extreme_year_low_range_pct", 0.05)
    )
    spread_pct_min = cfg["buy_spread_percentile_min"]
    pe_pct_max = cfg["buy_pe_percentile_max"]
    if near_low:
        spread_pct_min = max(
            0.0, spread_pct_min - cfg.get("buy_near_year_low_spread_relax", 0)
        )
        pe_pct_max = min(100.0, pe_pct_max + cfg.get("buy_near_year_low_pe_relax", 0))
    spread_min = cfg["buy_spread_min"]
    if near_low:
        spread_min = max(
            0.0,
            spread_min - cfg.get("buy_near_year_low_spread_min_relax", 0),
        )
    spread_abs_ok = spread > spread_min
    if extreme_low and not spread_abs_ok:
        spread_abs_ok = True

    if pd.isna(spread_percentile) or pd.isna(pe_percentile):
        if not (near_low and extreme_low):
            return False
        spread_pct_ok = (
            pd.isna(spread_percentile) or spread_percentile >= spread_pct_min
        )
        pe_ok = pd.isna(pe_percentile) or pe_percentile <= pe_pct_max
    else:
        spread_pct_ok = spread_percentile >= spread_pct_min
        pe_ok = pe_percentile <= pe_pct_max
    min_drawdown = effective_drawdown_threshold(
        cfg.get("buy_min_drawdown_from_high_pct"),
        year_range_position,
        cfg.get("buy_near_year_low_range_pct"),
    )
    max_above_low = effective_max_above_low_pct(
        cfg.get("buy_max_above_low_pct"),
        year_range_position,
        cfg.get("buy_near_year_low_range_pct"),
        cfg.get("buy_near_year_low_above_low_relax", 0),
        cfg.get("buy_mid_range_position_pct"),
        cfg.get("buy_mid_range_max_above_low_pct"),
    )
    base = spread_abs_ok and spread_pct_ok and pe_ok
    if not price_position_ok(pct_above_low, max_above_low):
        return False
    if not drawdown_from_high_ok(pct_below_high, min_drawdown):
        return False
    if not year_range_ok(year_range_position, cfg.get("buy_max_year_range_pct")):
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
        pct_below_high=row.get("pct_below_high"),
        year_range_position=row.get("year_range_position"),
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
    history_meta = panel_history_meta(panel)
    spread = latest["spread"]
    spread_percentile = latest["spread_percentile"]
    pe_percentile = latest["pe_percentile"]
    pct_above_low = latest.get("pct_above_low")

    spread_10y_pct = latest.get("spread_10y_percentile")
    spread_10y_min = latest.get("spread_10y_min")
    spread_10y_max = latest.get("spread_10y_max")
    spread_10y_sample = latest.get("spread_10y_sample_days")

    return {
        "pe": float(latest["pe"]),
        "dividend_yield": float(latest["dividend_yield"]),
        "index_date": latest["date"],
        "spread": spread,
        "spread_percentile": spread_percentile,
        "pe_percentile": pe_percentile,
        "spread_10y_percentile": (
            float(spread_10y_pct)
            if spread_10y_pct is not None and not pd.isna(spread_10y_pct)
            else None
        ),
        "spread_10y_min": (
            float(spread_10y_min)
            if spread_10y_min is not None and not pd.isna(spread_10y_min)
            else None
        ),
        "spread_10y_max": (
            float(spread_10y_max)
            if spread_10y_max is not None and not pd.isna(spread_10y_max)
            else None
        ),
        "spread_10y_sample_days": (
            int(spread_10y_sample)
            if spread_10y_sample is not None and not pd.isna(spread_10y_sample)
            else None
        ),
        "pct_above_low": (
            float(pct_above_low) if pct_above_low is not None and not pd.isna(pct_above_low) else None
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
        "is_buy": is_buy_signal_row(latest, index_code),
        "panel": panel,
        **history_meta,
        **row_price_position_fields(latest),
    }


def collect_index_results(indices, bond_history, bond_yield):
    """拉取指定指数列表的行情。"""
    from signal_format import log_fetch_start

    index_results = []
    for index in indices:
        log_fetch_start(index["name"], index["code"])
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
