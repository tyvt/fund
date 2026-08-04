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


def compute_historical_earnings_growth(
    panel,
    years=None,
    min_days=None,
):
    """由日度 close/PE 隐含盈利估算滚动 CAGR（对齐美股逻辑）。"""
    from config import (
        CYB_HISTORICAL_GROWTH,
        CYB_HISTORICAL_GROWTH_MIN_DAYS,
        CYB_HISTORICAL_GROWTH_YEARS,
    )

    years = years if years is not None else CYB_HISTORICAL_GROWTH_YEARS
    min_days = min_days if min_days is not None else CYB_HISTORICAL_GROWTH_MIN_DAYS
    if panel is None or panel.empty:
        return None
    if "close" not in panel.columns or "pe" not in panel.columns:
        return None
    work = panel.dropna(subset=["close", "pe"]).copy()
    if len(work) < min_days:
        return None
    work["implied_earnings"] = work["close"] / work["pe"]
    work = work[work["implied_earnings"] > 0]
    if len(work) < min_days:
        return None
    latest = work.iloc[-1]
    date_col = "date" if "date" in work.columns else "date_only"
    latest_dt = pd.Timestamp(latest[date_col])
    target = latest_dt - pd.DateOffset(years=years)
    past = work[pd.to_datetime(work[date_col]) <= target]
    if past.empty:
        return None
    start = past.iloc[-1]
    if start["implied_earnings"] <= 0:
        return None
    elapsed = (latest_dt - pd.Timestamp(start[date_col])).days / 365.25
    if elapsed <= 0:
        return None
    return float(
        (latest["implied_earnings"] / start["implied_earnings"]) ** (1 / elapsed) - 1
    )


def resolve_cyb_historical_growth(panel=None, snapshot=None):
    """优先滚动 5 年 CAGR，不足时回退 CYB_HISTORICAL_GROWTH；自动值不低于 floor。"""
    from config import (
        CYB_HISTORICAL_GROWTH,
        CYB_HISTORICAL_GROWTH_AUTO,
        CYB_HISTORICAL_GROWTH_FLOOR,
    )

    growth = None
    if snapshot is not None:
        g = snapshot.get("historical_growth")
        if g is not None and not (isinstance(g, float) and pd.isna(g)):
            growth = float(g)
    if growth is None and CYB_HISTORICAL_GROWTH_AUTO and panel is not None:
        growth = compute_historical_earnings_growth(panel)
    if growth is None:
        return CYB_HISTORICAL_GROWTH
    if CYB_HISTORICAL_GROWTH_AUTO and CYB_HISTORICAL_GROWTH_FLOOR is not None:
        growth = max(growth, CYB_HISTORICAL_GROWTH_FLOOR)
    return growth


def attach_historical_growth_series(panel, years=None, min_days=None):
    """为面板逐日附加截至当日的滚动盈利 CAGR（无未来函数）。"""
    import numpy as np
    from config import CYB_HISTORICAL_GROWTH_MIN_DAYS, CYB_HISTORICAL_GROWTH_YEARS

    years = years if years is not None else CYB_HISTORICAL_GROWTH_YEARS
    min_days = min_days if min_days is not None else CYB_HISTORICAL_GROWTH_MIN_DAYS
    if panel is None or panel.empty:
        return panel
    out = panel.copy()
    date_col = "date_only" if "date_only" in out.columns else "date"
    if "close" not in out.columns or "pe" not in out.columns:
        out["historical_growth"] = np.nan
        return out
    dts = pd.to_datetime(out[date_col])
    earns = (out["close"] / out["pe"]).to_numpy(dtype=float)
    n = len(out)
    growth = np.full(n, np.nan)
    # 用整数日序做 searchsorted，避免 datetime64 比较问题
    day_ord = dts.map(lambda x: x.toordinal()).to_numpy(dtype=np.int64)
    for i in range(min_days, n):
        e1 = earns[i]
        if not np.isfinite(e1) or e1 <= 0:
            continue
        target_ord = (dts.iloc[i] - pd.DateOffset(years=years)).toordinal()
        j = int(np.searchsorted(day_ord, target_ord, side="right") - 1)
        if j < 0:
            continue
        e0 = earns[j]
        if not np.isfinite(e0) or e0 <= 0:
            continue
        elapsed = (dts.iloc[i] - dts.iloc[j]).days / 365.25
        if elapsed <= 0:
            continue
        growth[i] = (e1 / e0) ** (1 / elapsed) - 1
    out["historical_growth"] = growth
    return out


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
    from data_cache import run_memo

    growth_key = "default" if expected_growth is None else str(expected_growth)
    return run_memo(f"cyb:{growth_key}", lambda: _fetch_cyb_snapshot_uncached(expected_growth))


def _fetch_cyb_snapshot_uncached(expected_growth=None):
    """拉取创业板指最新估值指标与历史分位（无进程内记忆）。"""
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
    panel = attach_historical_growth_series(panel)
    latest = panel.iloc[-1]
    volatility = compute_annualized_volatility(price_history)
    hist_growth = resolve_cyb_historical_growth(panel=panel, snapshot={"historical_growth": latest.get("historical_growth")})
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
        "historical_growth": hist_growth,
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
        last_buy_signal_price_from_column,
        row_field,
    )

    def _row_snap(row):
        return {
            "pe": row_field(row, "pe"),
            "pb": row_field(row, "pb"),
            "pe_percentile": row_field(row, "pe_percentile"),
            "pb_percentile": row_field(row, "pb_percentile"),
            "pct_above_low": row_field(row, "pct_above_low"),
            "pct_below_high": row_field(row, "pct_below_high"),
            "year_range_position": row_field(row, "year_range_position"),
            "ma_slope_pct": row_field(row, "ma_slope_pct"),
        }

    panel = attach_buy_signal_column(panel, evaluate_cyb_signal, _row_snap)
    snapshot["recent_signal_buy_avg"] = compute_recent_signal_buy_avg_from_column(
        panel,
        lookback_days=CYB_SELL_COST_LOOKBACK_DAYS,
    )
    snapshot["peak_since_last_buy"] = compute_peak_since_last_buy_from_column(panel)
    snapshot["last_buy_signal_price"] = last_buy_signal_price_from_column(panel)
    last_buy_date = last_buy_date_from_column(panel, date_col="date_only")
    if last_buy_date is not None:
        snapshot["days_since_last_buy"] = (
            pd.Timestamp(latest["date_only"]) - pd.Timestamp(last_buy_date)
        ).days
    return merge_history_meta(snapshot, panel, date_col="date_only")
