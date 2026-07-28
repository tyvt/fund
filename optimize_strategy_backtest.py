"""对比策略优化建议 vs 基线（2016-2025 十年回测）。"""

import copy
import sys
from dataclasses import dataclass

import pandas as pd

from backtest_buy_signals import (
    BacktestPanels,
    CN_BROAD_BACKTEST_INDICES,
    US_INDEX_META,
    _us_buy_snapshot,
)
from backtest_trade_signals import simulate_trades, _filter_panel
from cn_broad_signal import evaluate_cn_broad_buy, evaluate_cn_broad_sell
from config import (
    CYB_HISTORICAL_GROWTH,
    CYB_INDEX,
    HSTECH_HISTORICAL_GROWTH,
    HSTECH_INDEX,
    INDICES,
    US_INDEX_KEYS,
    resolve_backtest_amounts,
)
from cyb_signal import compute_peg, evaluate_cyb_signal
from dividend_data import is_buy_signal_row
from hstech_signal import evaluate_hstech_signal
from market_data import compute_percentile, configure_stdout_utf8
from us_index_signal import evaluate_signal, is_buy as is_us_index_buy

START = "2016-01-01"
END = "2025-12-31"


@dataclass
class VariantResult:
    name: str
    module: str
    code: str
    buy_count: int
    sell_count: int
    return_pct: float | None
    buy_only_return_pct: float | None
    baseline_return_pct: float | None
    baseline_buy_count: int
    note: str = ""


def _row_dict(row):
    return row.to_dict() if hasattr(row, "to_dict") else dict(row)


def _attach_payout_ratio(panel):
    """股息支付率 ≈ 股息率 × PE。"""
    out = panel.copy()
    out["payout_ratio"] = out["dividend_yield"] * out["pe"]
    pcts = []
    window, min_days = 756, 60
    for idx in range(len(out)):
        if idx < min_days:
            pcts.append(None)
            continue
        start = max(0, idx - window)
        hist = out["payout_ratio"].iloc[start:idx]
        pcts.append(compute_percentile(hist, out["payout_ratio"].iloc[idx]))
    out["payout_ratio_percentile"] = pcts
    return out


def _attach_erp(panel):
    out = panel.copy()
    out["erp"] = 1.0 / out["pe"] - out["bond_yield"]
    pcts = []
    window, min_days = 2520, 120
    for idx in range(len(out)):
        if idx < min_days:
            pcts.append(None)
            continue
        start = max(0, idx - window)
        hist = out["erp"].iloc[start:idx]
        pcts.append(compute_percentile(hist, out["erp"].iloc[idx]))
    out["erp_percentile"] = pcts
    return out


def _attach_pe_growth_1y(panel, date_col="date", pe_col=None):
    """用 PE 同比变化估算近 1 年盈利增速（保守）。"""
    out = panel.copy()
    if pe_col is None:
        if "pe" in out.columns:
            pe_col = "pe"
        elif "trailing_pe" in out.columns:
            pe_col = "trailing_pe"
        else:
            pe_col = "forward_pe"
    out = out.sort_values(date_col).reset_index(drop=True)
    growths = []
    for idx in range(len(out)):
        if idx < 252:
            growths.append(None)
            continue
        pe_now = out[pe_col].iloc[idx]
        pe_prev = out[pe_col].iloc[idx - 252]
        if pe_now and pe_prev and pe_now > 0 and pe_prev > 0:
            # earnings yield 变化 ≈ 盈利增速
            g = (pe_prev / pe_now) - 1.0
            growths.append(max(g, 0.01))
        else:
            growths.append(None)
    out["growth_1y"] = growths
    return out


def _conservative_growth(hist_growth, growth_1y):
    if hist_growth is None and growth_1y is None:
        return None
    if hist_growth is None:
        return growth_1y
    if growth_1y is None:
        return hist_growth
    return min(hist_growth, growth_1y)


# ── 基线信号 ──────────────────────────────────────────────


def baseline_dividend_buy(row, code):
    return is_buy_signal_row(row, code)


def baseline_cn_broad(row, code):
    ev = evaluate_cn_broad_buy(_cn_snap(row, code))
    return ev["is_buy"], ev.get("is_sell", False)


def baseline_cyb(row):
    ev = evaluate_cyb_signal(_cyb_snap(row))
    return ev["is_buy"], ev.get("is_sell", False)


def baseline_hstech(row):
    ev = evaluate_hstech_signal(_hstech_snap(row))
    return ev["is_buy"], ev.get("is_sell", False)


def baseline_us(row, key, growth):
    return _us_buy_snapshot(key, row, growth), False


def _cn_snap(row, code):
    d = _row_dict(row)
    d["code"] = code
    return d


def _cyb_snap(row):
    d = _row_dict(row)
    d["pe"] = row.get("pe")
    d["pb"] = row.get("pb")
    return d


def _hstech_snap(row):
    d = _row_dict(row)
    d["pe"] = row.get("pe")
    return d


# ── 优化变体 ──────────────────────────────────────────────


def variant_dividend_payout_filter(row, code, payout_max_pct=80):
    if not baseline_dividend_buy(row, code):
        return False
    pr = row.get("payout_ratio_percentile")
    if pr is None:
        return True
    return pr < payout_max_pct


def variant_cn_broad_price_weight(row, code):
    """年区间位置从硬门槛改为 20% 权重：估值通过即可，价格位置仅软约束。"""
    from price_position import year_range_ok
    from config import get_cn_broad_signal_config

    snap = _cn_snap(row, code)
    cfg = get_cn_broad_signal_config(code)
    ev = evaluate_cn_broad_buy(snap)
    year_range = row.get("year_range_position")
    yr_ok = year_range_ok(year_range, cfg.get("buy_max_year_range_pct"))

    # 复刻 evaluate_cn_broad_buy 但去掉 year_range 硬过滤
    if not ev["is_buy"] and yr_ok:
        return ev["is_buy"], ev.get("is_sell", False)
    if ev["is_buy"]:
        return True, ev.get("is_sell", False)

    # 估值项通过、仅年区间未过：按 20% 权重放宽
    # 重新评估：若去掉 year_range 硬门槛后 base_buy 成立，且 year_range 不太离谱
    relaxed_max = min(1.0, (cfg.get("buy_max_year_range_pct") or 0.5) + 0.15)
    soft_buy = _cn_broad_buy_without_year_range(snap) and (
        year_range is None or year_range <= relaxed_max
    )
    sell = evaluate_cn_broad_sell(snap)["is_sell"] and not soft_buy
    return soft_buy, sell


def _cn_broad_buy_without_year_range(snapshot):
    """cn_broad 买入逻辑，去掉 year_range 硬过滤。"""
    from config import get_cn_broad_signal_config
    from price_position import (
        drawdown_from_high_ok,
        effective_drawdown_threshold,
        effective_max_above_low_pct,
        is_near_year_low,
        price_position_ok,
        trend_filter_ok,
    )

    index_code = snapshot.get("code")
    cfg = get_cn_broad_signal_config(index_code)
    pe_pct = snapshot.get("pe_percentile")
    pb_pct = snapshot.get("pb_percentile")
    spread_pct = snapshot.get("spread_percentile")
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(year_range, cfg.get("buy_near_year_low_range_pct"))
    spread_min = cfg["buy_spread_percentile_min"]
    pe_max = cfg["buy_pe_percentile_max"]
    if near_low:
        spread_min = max(0.0, spread_min - cfg.get("buy_near_year_low_spread_relax", 0))
        pe_max = min(100.0, pe_max + cfg.get("buy_near_year_low_pe_relax", 0))
    max_above_low = effective_max_above_low_pct(
        cfg["buy_max_above_low_pct"],
        year_range,
        cfg.get("buy_near_year_low_range_pct"),
        cfg.get("buy_near_year_low_above_low_relax", 0),
        cfg.get("buy_mid_range_position_pct"),
        cfg.get("buy_mid_range_max_above_low_pct"),
    )
    min_drawdown = effective_drawdown_threshold(
        cfg.get("buy_min_drawdown_from_high_pct"),
        year_range,
        cfg.get("buy_near_year_low_drawdown_waive_pct"),
    )
    spread_ok = spread_pct is not None and spread_pct >= spread_min
    pe_ok = pe_pct is not None and pe_pct <= pe_max
    pb_ok = pb_pct is None or (pb_pct <= cfg["buy_pb_percentile_max"])
    criteria_count = sum(
        [
            spread_pct is not None,
            pe_pct is not None,
            pb_pct is not None,
        ]
    )
    score = sum([spread_ok, pe_ok, pb_ok if pb_pct is not None else False])
    total = criteria_count
    if total >= 3:
        need = max(cfg["buy_min_pass_score_floor"], total - 1)
    else:
        need = max(1, total)
    base_buy = (
        (spread_ok or not cfg["buy_require_spread"])
        and score >= need
        and total >= cfg["buy_min_applicable_criteria"]
    )
    return base_buy and price_position_ok(
        snapshot.get("pct_above_low"), max_above_low
    ) and drawdown_from_high_ok(
        snapshot.get("pct_below_high"), min_drawdown
    ) and trend_filter_ok(
        snapshot.get("ma_slope_pct"),
        year_range,
        cfg.get("buy_trend_min_ma_slope_pct"),
        cfg.get("buy_trend_downtrend_max_range_pct"),
    )


def variant_cyb_price_weight(row):
    """创业板：年区间位置软约束（同宽基思路）。"""
    from config import (
        CYB_BUY_MAX_YEAR_RANGE_PCT,
        CYB_BUY_PE_PERCENTILE_MAX,
        CYB_BUY_PB_PERCENTILE_MAX,
        CYB_BUY_PEG_HIST_MAX,
    )
    from price_position import (
        drawdown_from_high_ok,
        effective_drawdown_threshold,
        effective_max_above_low_pct,
        is_near_year_low,
        price_position_ok,
        trend_filter_ok,
        year_range_ok,
    )
    from config import (
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
        CYB_BUY_HIGH_LOOKBACK_DAYS,
        CYB_BUY_LOW_LOOKBACK_DAYS,
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
        CYB_BUY_MID_RANGE_POSITION_PCT,
        CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
        CYB_BUY_NEAR_YEAR_LOW_PE_RELAX,
        CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
        CYB_BUY_TREND_MIN_MA_SLOPE_PCT,
    )

    snap = _cyb_snap(row)
    pe = snap.get("pe")
    peg_historical = compute_peg(pe, CYB_HISTORICAL_GROWTH)
    pe_pct = snap.get("pe_percentile")
    pb_pct = snap.get("pb_percentile")
    year_range = snap.get("year_range_position")
    near_low = is_near_year_low(year_range, CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT)
    pe_max = CYB_BUY_PE_PERCENTILE_MAX
    pb_max = CYB_BUY_PB_PERCENTILE_MAX
    if near_low:
        pe_max = min(100.0, pe_max + CYB_BUY_NEAR_YEAR_LOW_PE_RELAX)
        pb_max = min(100.0, pb_max + CYB_BUY_NEAR_YEAR_LOW_PE_RELAX)
    max_above_low = effective_max_above_low_pct(
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        year_range,
        CYB_BUY_NEAR_YEAR_LOW_RANGE_PCT,
        BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
        CYB_BUY_MID_RANGE_POSITION_PCT,
        CYB_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT,
    )
    min_drawdown = effective_drawdown_threshold(
        CYB_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT,
        year_range,
        BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
    )
    pe_ok = pe_pct is not None and pe_pct <= pe_max
    pb_ok = pb_pct is not None and pb_pct <= pb_max
    peg_ok = peg_historical is not None and peg_historical <= CYB_BUY_PEG_HIST_MAX
    core = pe_ok and pb_ok and peg_ok
    pos_ok = (
        price_position_ok(snap.get("pct_above_low"), max_above_low)
        and drawdown_from_high_ok(snap.get("pct_below_high"), min_drawdown)
        and trend_filter_ok(
            snap.get("ma_slope_pct"),
            year_range,
            CYB_BUY_TREND_MIN_MA_SLOPE_PCT,
            CYB_BUY_TREND_DOWNTREND_MAX_RANGE_PCT,
        )
    )
    yr_ok = year_range_ok(year_range, CYB_BUY_MAX_YEAR_RANGE_PCT)
    relaxed_max = min(1.0, CYB_BUY_MAX_YEAR_RANGE_PCT + 0.12)
    soft_yr = year_range is None or year_range <= relaxed_max
    is_buy = core and pos_ok and (yr_ok or (soft_yr and core))
    ev = evaluate_cyb_signal(snap)
    is_sell = ev.get("is_sell", False) and not is_buy
    return is_buy, is_sell


def variant_conservative_peg_cyb(row):
    snap = _cyb_snap(row)
    growth = _conservative_growth(CYB_HISTORICAL_GROWTH, row.get("growth_1y"))
    peg = compute_peg(snap.get("pe"), growth)
    from config import CYB_BUY_PEG_HIST_MAX

    ev = evaluate_cyb_signal(snap)
    if not ev["is_buy"]:
        return ev["is_buy"], ev.get("is_sell", False)
    if peg is None:
        return True, ev.get("is_sell", False)
    peg_ok = peg <= CYB_BUY_PEG_HIST_MAX
    return peg_ok, ev.get("is_sell", False) and not peg_ok


def variant_conservative_peg_hstech(row):
    snap = _hstech_snap(row)
    growth = _conservative_growth(HSTECH_HISTORICAL_GROWTH, row.get("growth_1y"))
    peg = compute_peg(snap.get("pe"), growth)
    from config import HSTECH_BUY_PEG_HIST_MAX

    ev = evaluate_hstech_signal(snap)
    if not ev["is_buy"]:
        return ev["is_buy"], ev.get("is_sell", False)
    if peg is None:
        return True, ev.get("is_sell", False)
    peg_ok = peg <= HSTECH_BUY_PEG_HIST_MAX
    return peg_ok, ev.get("is_sell", False) and not peg_ok


def variant_us_conservative_peg(row, key, growth):
    snap = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": growth,
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
    }
    from us_index_signal import resolve_expected_growth, compute_peg
    import config

    expected = resolve_expected_growth(key, snap)
    conservative = _conservative_growth(expected, row.get("growth_1y"))
    forward_pe = row.get("forward_pe")
    peg = compute_peg(forward_pe, conservative)
    peg_max = getattr(config, f"{key.upper()}_BUY_PEG_FORWARD_MAX")
    base_buy = is_us_index_buy(key, {**snap, "expected_growth": expected})
    if not base_buy:
        return False, False
    if peg is None:
        return True, False
    return peg <= peg_max, False


def variant_cn_broad_erp_buy(row, code, erp_pct_min=70):
    """ERP 分位高时放宽买入（极值反转）。"""
    is_buy, is_sell = baseline_cn_broad(row, code)
    if is_buy:
        return is_buy, is_sell
    erp_pct = row.get("erp_percentile")
    if erp_pct is None or erp_pct < erp_pct_min:
        return False, is_sell
    # ERP 极高时，若估值评分接近通过，允许买入
    if _cn_broad_buy_without_year_range(_cn_snap(row, code)):
        return True, False
    return False, is_sell


def variant_cn_broad_trend_stoploss_sell(row, code):
    """卖出增加趋势破位止损。"""
    is_buy, is_sell = baseline_cn_broad(row, code)
    if is_buy:
        return True, False
    close = row.get("close")
    ma = row.get("ma200")
    slope = row.get("ma_slope_pct")
    trend_break = (
        close is not None
        and ma is not None
        and slope is not None
        and close < ma
        and slope < 0
    )
    return False, is_sell or trend_break


def variant_us_high_rate_adapt(row, key, growth):
    """高利率环境：利率分位放宽至 95，但要求 PE 分位 ≤ 60。"""
    import config

    snap = {
        "forward_pe": row.get("forward_pe"),
        "trailing_pe": row.get("trailing_pe"),
        "forward_pe_percentile": row.get("forward_pe_percentile"),
        "trailing_pe_percentile": row.get("trailing_pe_percentile"),
        "us10y_percentile": row.get("us10y_percentile"),
        "implied_growth": row.get("implied_growth"),
        "historical_growth": growth,
        "pct_above_low": row.get("pct_above_low"),
        "pct_below_high": row.get("pct_below_high"),
        "year_range_position": row.get("year_range_position"),
        "ma_slope_pct": row.get("ma_slope_pct"),
    }
    from us_index_signal import resolve_expected_growth, evaluate_signal

    snap["expected_growth"] = resolve_expected_growth(key, snap)
    ev = evaluate_signal(key, snap)
    if ev["is_buy"]:
        return True, False
    rate_pct = row.get("us10y_percentile")
    fwd_pct = row.get("forward_pe_percentile")
    trl_pct = row.get("trailing_pe_percentile")
    pe_pct = fwd_pct if fwd_pct is not None else trl_pct
    if rate_pct is None or pe_pct is None:
        return False, False
    if rate_pct <= 95 and pe_pct <= 55:
        # 高利率但估值极低时允许买入
        return is_us_index_buy(key, snap) or (
            pe_pct <= 55 and rate_pct <= 95 and _us_relaxed_buy(key, snap)
        ), False
    return False, False


def _us_relaxed_buy(key, snap):
    """仅检查 PE+PEG+价格位置，不检查利率。"""
    from us_index_signal import evaluate_signal

    # 临时忽略利率：复制 evaluate_signal 核心但 rate_ok=True
    ev = evaluate_signal(key, snap)
    rate_pct = snap.get("us10y_percentile")
    if rate_pct is not None and rate_pct > 85:
        # 重新用更宽利率门槛
        import config
        from price_position import (
            drawdown_from_high_ok,
            effective_drawdown_threshold,
            effective_max_above_low_pct,
            is_near_year_low,
            price_position_ok,
            trend_filter_ok,
            year_range_ok,
        )
        from config import BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX, BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT, BUY_RANGE_LOOKBACK_DAYS

        year_range = snap.get("year_range_position")
        near_low = is_near_year_low(
            year_range, getattr(config, f"{key.upper()}_BUY_NEAR_YEAR_LOW_RANGE_PCT")
        )
        pe_threshold = getattr(config, f"{key.upper()}_BUY_FORWARD_PE_PERCENTILE_MAX")
        if near_low:
            pe_threshold = min(100.0, pe_threshold + getattr(config, f"{key.upper()}_BUY_NEAR_YEAR_LOW_PE_RELAX"))
        fwd_pct = snap.get("forward_pe_percentile")
        trl_pct = snap.get("trailing_pe_percentile")
        pe_ok = (fwd_pct is not None and fwd_pct <= pe_threshold) or (
            fwd_pct is None and trl_pct is not None and trl_pct <= getattr(config, f"{key.upper()}_BUY_TRAILING_PE_PERCENTILE_MAX")
        )
        max_above_low = effective_max_above_low_pct(
            getattr(config, f"{key.upper()}_BUY_MAX_ABOVE_LOW_PCT"),
            year_range,
            getattr(config, f"{key.upper()}_BUY_NEAR_YEAR_LOW_RANGE_PCT"),
            BUY_NEAR_YEAR_LOW_ABOVE_LOW_RELAX,
            getattr(config, f"{key.upper()}_BUY_MID_RANGE_POSITION_PCT"),
            getattr(config, f"{key.upper()}_BUY_MID_RANGE_MAX_ABOVE_LOW_PCT"),
        )
        min_drawdown = effective_drawdown_threshold(
            getattr(config, f"{key.upper()}_BUY_MIN_DRAWDOWN_FROM_HIGH_PCT"),
            year_range,
            BUY_NEAR_YEAR_LOW_DRAWDOWN_WAIVE_PCT,
        )
        return pe_ok and pe_ok and price_position_ok(
            snap.get("pct_above_low"), max_above_low
        ) and drawdown_from_high_ok(
            snap.get("pct_below_high"), min_drawdown
        ) and year_range_ok(
            year_range, getattr(config, f"{key.upper()}_BUY_MAX_YEAR_RANGE_PCT")
        ) and trend_filter_ok(
            snap.get("ma_slope_pct"),
            year_range,
            getattr(config, f"{key.upper()}_BUY_TREND_MIN_MA_SLOPE_PCT"),
            getattr(config, f"{key.upper()}_BUY_TREND_DOWNTREND_MAX_RANGE_PCT"),
        )
    return ev["is_buy"]


# ── 自定义模拟（部分卖出 / 成本止盈）──────────────────────


def simulate_partial_sell(
    panel,
    start_date,
    end_date,
    amount,
    buy_fn,
    sell_fn,
    sell_fraction=0.5,
    date_col="date",
    valuation_price_col=None,
):
    val_col = valuation_price_col or "close"
    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty:
        return None
    if val_col not in sample.columns:
        val_col = "close"
    latest = sample.iloc[-1]
    latest_price = float(latest[val_col])

    units = 0.0
    buy_only_units = 0.0
    total_bought = 0.0
    total_sold = 0.0
    buy_count = 0
    sell_count = 0

    for _, row in sample.iterrows():
        price = float(row[val_col])
        is_buy = buy_fn(row)
        is_sell = sell_fn(row) if units > 0 else False
        if is_buy:
            units += amount / price
            buy_only_units += amount / price
            total_bought += amount
            buy_count += 1
        elif is_sell and units > 0:
            sell_units = units * sell_fraction
            total_sold += sell_units * price
            units -= sell_units
            sell_count += 1

    final_value = total_sold + units * latest_price
    profit = final_value - total_bought
    return_pct = profit / total_bought * 100 if total_bought > 0 else None
    buy_only_value = buy_only_units * latest_price
    buy_only_profit = buy_only_value - total_bought
    buy_only_return_pct = (
        buy_only_profit / total_bought * 100 if total_bought > 0 else None
    )
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "return_pct": return_pct,
        "buy_only_return_pct": buy_only_return_pct,
        "total_bought": total_bought,
    }


def simulate_hstech_cost_sell(
    panel, start_date, end_date, amount, profit_pct=0.25, date_col="date"
):
    sample = _filter_panel(panel, start_date, end_date, date_col=date_col)
    if sample.empty:
        return None
    latest_price = float(sample.iloc[-1]["close"])

    units = 0.0
    buy_only_units = 0.0
    total_bought = 0.0
    total_sold = 0.0
    buy_count = 0
    sell_count = 0
    cost_basis = 0.0

    for _, row in sample.iterrows():
        price = float(row["close"])
        ev = evaluate_hstech_signal(_hstech_snap(row))
        is_buy = ev["is_buy"]
        val_sell = ev.get("is_sell", False)
        avg_cost = cost_basis / units if units > 0 else None
        is_sell = (
            val_sell
            and units > 0
            and avg_cost is not None
            and price >= avg_cost * (1 + profit_pct)
        )
        if is_buy:
            units += amount / price
            buy_only_units += amount / price
            cost_basis += amount
            total_bought += amount
            buy_count += 1
        elif is_sell:
            total_sold += units * price
            units = 0.0
            cost_basis = 0.0
            sell_count += 1

    final_value = total_sold + units * latest_price
    profit = final_value - total_bought
    return_pct = profit / total_bought * 100 if total_bought > 0 else None
    buy_only_value = buy_only_units * latest_price
    buy_only_return_pct = (
        (buy_only_value - total_bought) / total_bought * 100
        if total_bought > 0
        else None
    )
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "return_pct": return_pct,
        "buy_only_return_pct": buy_only_return_pct,
        "total_bought": total_bought,
    }


def _run_sim(panel, start, end, amount, buy_fn, sell_fn=None, has_sell=False, **kw):
    if kw.get("partial_sell"):
        partial_kw = {
            k: kw[k]
            for k in ("sell_fraction", "date_col", "valuation_price_col")
            if k in kw
        }
        return simulate_partial_sell(
            panel, start, end, amount, buy_fn, sell_fn, **partial_kw
        )
    if kw.get("hstech_cost"):
        cost_kw = {k: kw[k] for k in ("profit_pct", "date_col") if k in kw}
        return simulate_hstech_cost_sell(panel, start, end, amount, **cost_kw)
    stats = simulate_trades(
        panel,
        start,
        end,
        amount=amount,
        buy_fn=buy_fn,
        sell_fn=sell_fn,
        has_sell=has_sell,
        valuation_price_col=kw.get("valuation_price_col"),
    )
    if not stats:
        return None
    return {
        "buy_count": stats["buy_count"],
        "sell_count": stats["sell_count"],
        "return_pct": stats["return_pct"],
        "buy_only_return_pct": stats["buy_only_return_pct"],
        "total_bought": stats["total_bought"],
    }


def _compare(name, module, code, variant_stats, baseline_stats, note=""):
    if not variant_stats or not baseline_stats:
        return None
    vr = variant_stats.get("return_pct") or variant_stats.get("buy_only_return_pct")
    br = baseline_stats.get("return_pct") or baseline_stats.get("buy_only_return_pct")
    return VariantResult(
        name=name,
        module=module,
        code=code,
        buy_count=variant_stats["buy_count"],
        sell_count=variant_stats.get("sell_count", 0),
        return_pct=vr,
        buy_only_return_pct=variant_stats.get("buy_only_return_pct"),
        baseline_return_pct=br,
        baseline_buy_count=baseline_stats["buy_count"],
        note=note,
    )


def main():
    configure_stdout_utf8()
    print(f"加载数据并回测 {START} ~ {END} ...")
    panels = BacktestPanels()
    amounts = resolve_backtest_amounts()
    results: list[VariantResult] = []

    # ── 红利：派息率过滤 ──
    for item in INDICES:
        code = item["code"]
        panel = _attach_payout_ratio(panels.dividend_panel(code))
        bl = _run_sim(
            panel,
            START,
            END,
            amounts["dividend"],
            lambda r, c=code: baseline_dividend_buy(r, c),
            valuation_price_col="total_return_close",
        )
        var = _run_sim(
            panel,
            START,
            END,
            amounts["dividend"],
            lambda r, c=code: variant_dividend_payout_filter(r, c),
            valuation_price_col="total_return_close",
        )
        r = _compare(
            "派息率<80%分位过滤",
            "红利",
            code,
            var,
            bl,
            "过滤股息陷阱（支付率分位过高）",
        )
        if r:
            results.append(r)

    # ── 宽基：价格位置软化 / ERP / 趋势止损 ──
    for item in CN_BROAD_BACKTEST_INDICES:
        code = item["code"]
        panel = panels.cn_broad_panel(code)
        panel_erp = _attach_erp(panel)
        panel_ma = panel.copy()
        if "ma200" not in panel_ma.columns and "close" in panel_ma.columns:
            panel_ma["ma200"] = panel_ma["close"].rolling(200, min_periods=60).mean()

        bl = _run_sim(
            panel,
            START,
            END,
            amounts["cn_broad"],
            lambda r, c=code: baseline_cn_broad(r, c)[0],
            sell_fn=lambda r, c=code: baseline_cn_broad(r, c)[1],
            has_sell=True,
        )

        var_pw = _run_sim(
            panel,
            START,
            END,
            amounts["cn_broad"],
            lambda r, c=code: variant_cn_broad_price_weight(r, c)[0],
            sell_fn=lambda r, c=code: variant_cn_broad_price_weight(r, c)[1],
            has_sell=True,
        )
        r = _compare("年区间位置软化(20%权重)", "宽基", code, var_pw, bl)
        if r:
            results.append(r)

        var_erp = _run_sim(
            panel_erp,
            START,
            END,
            amounts["cn_broad"],
            lambda r, c=code: variant_cn_broad_erp_buy(r, c)[0],
            sell_fn=lambda r, c=code: variant_cn_broad_erp_buy(r, c)[1],
            has_sell=True,
        )
        r = _compare("ERP极值买入", "宽基", code, var_erp, bl, "ERP分位≥70%时放宽")
        if r:
            results.append(r)

        var_ts = _run_sim(
            panel_ma,
            START,
            END,
            amounts["cn_broad"],
            lambda r, c=code: variant_cn_broad_trend_stoploss_sell(r, c)[0],
            sell_fn=lambda r, c=code: variant_cn_broad_trend_stoploss_sell(r, c)[1],
            has_sell=True,
        )
        r = _compare("趋势破位止损", "宽基", code, var_ts, bl)
        if r:
            results.append(r)

    # ── 创业板 ──
    cyb_panel = _attach_pe_growth_1y(panels.cyb_panel(), date_col="date")
    bl_cyb = _run_sim(
        cyb_panel,
        START,
        END,
        amounts["other"],
        lambda r: baseline_cyb(r)[0],
        sell_fn=lambda r: baseline_cyb(r)[1],
        has_sell=True,
        date_col="date",
    )
    for name, buy_fn in [
        ("年区间位置软化", lambda r: variant_cyb_price_weight(r)[0]),
        ("保守PEG(min 5y/1y)", lambda r: variant_conservative_peg_cyb(r)[0]),
    ]:
        var = _run_sim(
            cyb_panel,
            START,
            END,
            amounts["other"],
            buy_fn,
            sell_fn=lambda r: baseline_cyb(r)[1],
            has_sell=True,
            date_col="date",
        )
        r = _compare(name, "创业板", CYB_INDEX["code"], var, bl_cyb)
        if r:
            results.append(r)

    # ── 恒科：成本+25%卖出 ──
    hstech_panel = _attach_pe_growth_1y(panels.hstech_panel(), date_col="date")
    bl_hs = _run_sim(
        hstech_panel,
        START,
        END,
        amounts["other"],
        lambda r: baseline_hstech(r)[0],
        sell_fn=lambda r: baseline_hstech(r)[1],
        has_sell=True,
        date_col="date",
    )
    var_hs = simulate_hstech_cost_sell(
        hstech_panel, START, END, amounts["other"], profit_pct=0.25, date_col="date"
    )
    r = _compare(
        "卖出需盈利≥25%",
        "恒科",
        HSTECH_INDEX["code"],
        var_hs,
        bl_hs,
        "估值卖出+成本价前置",
    )
    if r:
        results.append(r)

    var_peg_hs = _run_sim(
        hstech_panel,
        START,
        END,
        amounts["other"],
        lambda r: variant_conservative_peg_hstech(r)[0],
        sell_fn=lambda r: variant_conservative_peg_hstech(r)[1],
        has_sell=True,
        date_col="date",
    )
    r = _compare(
        "保守PEG(min 5y/1y)",
        "恒科",
        HSTECH_INDEX["code"],
        var_peg_hs,
        bl_hs,
    )
    if r:
        results.append(r)

    # ── 美股：部分卖出 / 高利率适应 / 保守PEG ──
    for key in US_INDEX_KEYS:
        daily, growth = panels.us_index_panel(key)
        us_panel = _attach_pe_growth_1y(daily)
        meta = US_INDEX_META[key]
        code = meta["code"]

        bl_us = _run_sim(
            us_panel,
            START,
            END,
            amounts["other"],
            lambda r, k=key, g=growth: baseline_us(r, k, g)[0],
        )

        def us_sell_fn(row, k=key):
            pe_pct = row.get("forward_pe_percentile") or row.get(
                "trailing_pe_percentile"
            )
            rate_pct = row.get("us10y_percentile")
            return (
                pe_pct is not None
                and rate_pct is not None
                and pe_pct > 80
                and rate_pct > 70
            )

        var_sell = _run_sim(
            us_panel,
            START,
            END,
            amounts["other"],
            lambda r, k=key, g=growth: baseline_us(r, k, g)[0],
            sell_fn=us_sell_fn,
            partial_sell=True,
            sell_fraction=0.5,
        )
        r = _compare(
            "PE>80%且利率>70%减持50%",
            "美股",
            code,
            var_sell,
            bl_us,
            "卫星仓位减持，非清仓",
        )
        if r:
            results.append(r)

        var_rate = _run_sim(
            us_panel,
            START,
            END,
            amounts["other"],
            lambda r, k=key, g=growth: variant_us_high_rate_adapt(r, k, g)[0],
        )
        r = _compare("高利率环境适应", "美股", code, var_rate, bl_us)
        if r:
            results.append(r)

        var_peg = _run_sim(
            us_panel,
            START,
            END,
            amounts["other"],
            lambda r, k=key, g=growth: variant_us_conservative_peg(r, k, g)[0],
        )
        r = _compare("保守PEG(min fwd/1y)", "美股", code, var_peg, bl_us)
        if r:
            results.append(r)

    # ── 输出汇总 ──
    print("\n" + "=" * 100)
    print(f"{'优化项':<28} {'模块':<6} {'代码':<8} {'买次Δ':>7} {'收益率':>8} {'基线':>8} {'Δ收益':>8} 建议")
    print("-" * 100)

    adopt = []
    reject = []
    neutral = []

    for r in results:
        buy_delta = r.buy_count - r.baseline_buy_count
        buy_delta_pct = (
            buy_delta / r.baseline_buy_count * 100
            if r.baseline_buy_count > 0
            else 0
        )
        ret_delta = (r.return_pct or 0) - (r.baseline_return_pct or 0)

        # 采纳标准：收益提升≥2pp 且买入次数降幅<30%；或收益提升≥5pp
        if ret_delta >= 2 and buy_delta_pct >= -30:
            verdict = "✅ 可考虑"
            adopt.append(r)
        elif ret_delta < -1 or buy_delta_pct < -40:
            verdict = "❌ 不建议"
            reject.append(r)
        else:
            verdict = "➖ 中性"
            neutral.append(r)

        print(
            f"{r.name:<28} {r.module:<6} {r.code:<8} "
            f"{buy_delta:+4d}({buy_delta_pct:+.0f}%) "
            f"{r.return_pct:>+7.1f}% {r.baseline_return_pct:>+7.1f}% "
            f"{ret_delta:>+7.1f}pp {verdict}"
        )

    print("=" * 100)
    print(f"\n可考虑采纳: {len(adopt)} 项 | 中性: {len(neutral)} 项 | 不建议: {len(reject)} 项")
    return 0


if __name__ == "__main__":
    sys.exit(main())
