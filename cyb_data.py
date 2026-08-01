"""创业板指（399006）估值数据拉取与历史分位计算。"""

import akshare as ak
import pandas as pd

from config import (
    BUY_RANGE_LOOKBACK_DAYS,
    BUY_TREND_MA_DAYS,
    BUY_TREND_SLOPE_LOOKBACK_DAYS,
    CYB_BUY_HIGH_LOOKBACK_DAYS,
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_BUY_MAX_YEAR_RANGE_PCT,
    CYB_DIV_PERCENTILE_WINDOW,
    CYB_INDEX,
    CYB_PERCENTILE_MIN_DAYS,
    CYB_PERCENTILE_WINDOW,
)
from data_cache import get_or_fetch_dataframe
from market_data import compute_percentile, rolling_percentile_series
from price_position import (
    attach_ma_trend,
    attach_pct_above_low,
    attach_pct_below_high,
    attach_year_range_position,
    row_price_position_fields,
)
from signal_format import merge_history_meta

CYB_CODE = CYB_INDEX["code"]
CYB_NAME = CYB_INDEX["name"]
CYB_DAILY_SYMBOL = "sz399006"


def fetch_cyb_pe_history():
    """乐咕乐股创业板板块滚动市盈率（月度）。"""
    def _fetch():
        df = ak.stock_market_pe_lg(symbol="创业板")
        out = df.rename(columns={"日期": "date", "平均市盈率": "pe"})
        out["date"] = pd.to_datetime(out["date"])
        out["pe"] = pd.to_numeric(out["pe"], errors="coerce")
        return out.dropna(subset=["date", "pe"]).sort_values("date").reset_index(drop=True)

    return get_or_fetch_dataframe("cyb_pe", _fetch, subdir="cyb")


def fetch_cyb_pb_history():
    """乐咕乐股创业板板块市净率（加权/等权/中位数，日度）。"""
    def _fetch():
        df = ak.stock_market_pb_lg(symbol="创业板")
        out = df.rename(
            columns={
                "日期": "date",
                "市净率": "pb",
                "等权市净率": "pb_equal",
                "市净率中位数": "pb_median",
            }
        )
        out["date"] = pd.to_datetime(out["date"])
        for col in ("pb", "pb_equal", "pb_median"):
            out[col] = pd.to_numeric(out[col], errors="coerce")
        return out.dropna(subset=["date", "pb"]).sort_values("date").reset_index(drop=True)

    return get_or_fetch_dataframe("cyb_pb", _fetch, subdir="cyb")


def fetch_cyb_dividend_history():
    """乐咕乐股创业板板块股息率（日度，单位为百分比数值）。"""
    def _fetch():
        df = ak.stock_a_gxl_lg(symbol="创业板")
        out = df.rename(columns={"日期": "date", "股息率": "dividend_yield"})
        out["date"] = pd.to_datetime(out["date"])
        out["dividend_yield"] = pd.to_numeric(out["dividend_yield"], errors="coerce") / 100
        return out.dropna(subset=["date", "dividend_yield"]).sort_values("date").reset_index(
            drop=True
        )

    return get_or_fetch_dataframe("cyb_dividend", _fetch, subdir="cyb")


def fetch_cyb_price_history():
    """创业板指日线收盘价（用于波动率）。"""
    def _fetch():
        df = ak.stock_zh_index_daily(symbol=CYB_DAILY_SYMBOL)
        out = df.rename(columns={"date": "date", "close": "close"})
        out["date"] = pd.to_datetime(out["date"])
        out["close"] = pd.to_numeric(out["close"], errors="coerce")
        return out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    return get_or_fetch_dataframe("cyb_price", _fetch, subdir="cyb")


def build_cyb_valuation_panel():
    """合并 PE、PB、股息率为日度估值面板。

    乐咕 PE 为月度发布，merge_asof 对齐后按指数收盘价折算为日度 PE，
    避免指数上涨而 PE 未更新导致分位与 PEG 被低估（如 2025 年 2 月）。
    """
    pb = fetch_cyb_pb_history()
    pe = fetch_cyb_pe_history()
    dividend = fetch_cyb_dividend_history()
    prices = fetch_cyb_price_history()

    panel = pb.sort_values("date")
    pe_src = pe.sort_values("date").rename(
        columns={"date": "pe_source_date", "pe": "pe_official"}
    )
    panel = pd.merge_asof(
        panel,
        pe_src,
        left_on="date",
        right_on="pe_source_date",
        direction="backward",
    )
    panel = pd.merge_asof(
        panel,
        dividend[["date", "dividend_yield"]].sort_values("date"),
        on="date",
        direction="backward",
    )
    panel = pd.merge_asof(
        panel.sort_values("date"),
        prices[["date", "close"]].sort_values("date"),
        on="date",
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
    panel = panel.dropna(subset=["pe", "pb", "pb_equal", "dividend_yield"])
    panel["date_only"] = panel["date"].dt.date
    drop_cols = ["pe_source_date", "close_at_pe_source", "close"]
    panel = panel.drop(columns=[c for c in drop_cols if c in panel.columns])
    return panel.reset_index(drop=True)


def compute_annualized_volatility(price_history, window=252):
    """基于日收益率计算年化波动率。"""
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
    window=CYB_PERCENTILE_WINDOW,
    div_window=CYB_DIV_PERCENTILE_WINDOW,
    min_days=CYB_PERCENTILE_MIN_DAYS,
):
    """计算 PE、PB、股息率滚动历史分位。"""
    if panel is None or panel.empty:
        return None

    out = panel.copy()
    out["pe_percentile"] = rolling_percentile_series(out["pe"], window, min_days)
    out["pb_percentile"] = rolling_percentile_series(out["pb"], window, min_days)
    out["pb_equal_percentile"] = rolling_percentile_series(
        out["pb_equal"], window, min_days
    )
    out["pb_median_percentile"] = rolling_percentile_series(
        out["pb_median"], window, min_days
    )
    out["dividend_percentile"] = rolling_percentile_series(
        out["dividend_yield"], div_window, min_days
    )
    return out


def fetch_cyb_snapshot(expected_growth=None):
    """拉取创业板指最新估值指标与历史分位。"""
    panel = build_cyb_valuation_panel()
    if panel is None or panel.empty:
        raise RuntimeError("无法构建创业板指估值历史序列")

    panel = attach_percentiles(panel)
    price_history = fetch_cyb_price_history()
    price_history["date_only"] = pd.to_datetime(price_history["date"]).dt.date
    panel = panel.merge(
        price_history[["date_only", "close"]],
        on="date_only",
        how="left",
    )
    panel = attach_pct_above_low(panel, lookback_days=CYB_BUY_LOW_LOOKBACK_DAYS)
    panel = attach_pct_below_high(
        panel, lookback_days=CYB_BUY_HIGH_LOOKBACK_DAYS
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

    pe = float(latest["pe"])
    pb = float(latest["pb"])
    pb_equal = float(latest["pb_equal"])
    pb_median = float(latest["pb_median"])
    dividend_yield = float(latest["dividend_yield"])

    snapshot = {
        "code": CYB_CODE,
        "name": CYB_NAME,
        "date": latest["date_only"],
        "close": float(latest["close"]),
        "pe": pe,
        "pb": pb,
        "pb_equal": pb_equal,
        "pb_median": pb_median,
        "dividend_yield": dividend_yield,
        "pe_percentile": latest["pe_percentile"],
        "pb_percentile": latest["pb_percentile"],
        "pb_equal_percentile": latest["pb_equal_percentile"],
        "pb_median_percentile": latest["pb_median_percentile"],
        "dividend_percentile": latest["dividend_percentile"],
        "pct_above_low": pct_above_low,
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
        "volatility": volatility,
        "history_days": int(panel["pe"].notna().sum()),
        "panel": panel,
        "expected_growth": expected_growth,
        "high_lookback_days": CYB_BUY_HIGH_LOOKBACK_DAYS,
        **row_price_position_fields(latest),
    }
    from cyb_signal import evaluate_cyb_signal
    from config import CYB_SELL_COST_LOOKBACK_DAYS
    from sell_trailing import (
        attach_buy_signal_column,
        compute_peak_since_last_buy_from_column,
        compute_recent_signal_buy_avg_from_column,
        last_buy_date_from_column,
    )

    def _row_snap(row):
        return {
            "pe": row["pe"],
            "pb": row["pb"],
            "pe_percentile": row.get("pe_percentile"),
            "pb_percentile": row.get("pb_percentile"),
            "pct_above_low": row.get("pct_above_low"),
            "pct_below_high": row.get("pct_below_high"),
            "year_range_position": row.get("year_range_position"),
            "ma_slope_pct": row.get("ma_slope_pct"),
        }

    panel = attach_buy_signal_column(panel, evaluate_cyb_signal, _row_snap)
    snapshot["recent_signal_buy_avg"] = compute_recent_signal_buy_avg_from_column(
        panel,
        lookback_days=CYB_SELL_COST_LOOKBACK_DAYS,
    )
    snapshot["peak_since_last_buy"] = compute_peak_since_last_buy_from_column(panel)
    last_buy_date = last_buy_date_from_column(panel, date_col="date_only")
    if last_buy_date is not None:
        snapshot["days_since_last_buy"] = (
            pd.Timestamp(latest["date_only"]) - pd.Timestamp(last_buy_date)
        ).days
    return merge_history_meta(snapshot, panel, date_col="date_only")
