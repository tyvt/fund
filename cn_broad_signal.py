"""A 股宽基指数买入/卖出信号与报告格式化。"""

import numpy as np
import pandas as pd

from buy_amount_config import enrich_signal_buy_amount
from config import cn_broad_sell_enabled, cn_broad_valuation_sell_enabled, get_cn_broad_signal_config
from drop_to_buy import (
    cn_broad_drop_to_buy,
    cn_broad_sell_trigger,
    format_buy_trigger_line,
    format_sell_trigger_line,
)
from price_position import (
    build_buy_price_ceilings,
    build_sell_price_floors,
    drawdown_from_high_ok,
    effective_drawdown_threshold,
    effective_max_above_low_pct,
    format_index_price,
    format_price_position_line,
    is_near_year_low,
    make_drawdown_from_high_criterion,
    make_price_position_criterion,
    make_sell_price_position_criterion,
    make_trend_criterion,
    make_year_range_criterion,
    price_position_ok,
    price_position_sell_hit,
    trend_filter_ok,
    year_range_ok,
)
from sell_trailing import trailing_sell_hit, valuation_sell_hit_cn_broad
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    append_signal_block,
    format_data_meta_line,
    make_criterion,
    pct_text,
)


def _resolve_cn_broad_signal_short(is_buy, is_sell):
    if is_buy:
        return SIGNAL_BUY
    if is_sell:
        return SIGNAL_SELL
    return SIGNAL_HOLD


def evaluate_cn_broad_sell(snapshot):
    """波段卖出：移动止盈（浮盈达标后峰值回撤）或估值偏高。"""
    index_code = snapshot.get("code")
    if not cn_broad_sell_enabled(index_code):
        return {
            "is_sell": False,
            "sell_criteria": [],
            "sell_summary": None,
        }
    cfg = get_cn_broad_signal_config(index_code)

    close = snapshot.get("close")
    recent_avg = snapshot.get("recent_signal_buy_avg")
    peak_price = snapshot.get("peak_since_last_buy")
    days_since_buy = snapshot.get("days_since_last_buy")

    trail_hit = trailing_sell_hit(
        close=close,
        cost_basis=recent_avg,
        peak_price=peak_price,
        min_unrealized_gain_pct=cfg.get("sell_min_unrealized_gain_pct"),
        trailing_drawdown_pct=cfg.get("sell_trailing_drawdown_pct"),
        min_hold_days=cfg.get("sell_trailing_min_hold_days"),
        days_since_buy=days_since_buy,
    )

    pe_pct = snapshot.get("pe_percentile")
    pb_pct = snapshot.get("pb_percentile")
    spread_pct = snapshot.get("spread_percentile")
    pct_above_low = snapshot.get("pct_above_low")
    lookback = cfg["buy_low_lookback_days"]

    spread_hit = (
        spread_pct is not None
        and spread_pct <= cfg["sell_spread_percentile_max"]
    )
    pe_hit = pe_pct is not None and pe_pct >= cfg["sell_pe_percentile_min"]
    pb_hit = (
        pb_pct is not None and pb_pct >= cfg["sell_pb_percentile_min"]
    )
    price_hit = price_position_sell_hit(
        pct_above_low, cfg["sell_max_above_low_pct"]
    )
    val_hit = (
        cn_broad_valuation_sell_enabled(cfg)
        and valuation_sell_hit_cn_broad(snapshot, cfg)
    )
    year_range = snapshot.get("year_range_position")
    min_range = cfg.get("sell_min_year_range_pct")
    combo_pe_min = cfg.get("sell_pe_combo_min")
    range_hit = (
        min_range is not None
        and year_range is not None
        and year_range >= min_range
    )
    combo_pe_hit = (
        combo_pe_min is not None
        and pe_pct is not None
        and pe_pct >= combo_pe_min
    )
    combo_hit = range_hit and combo_pe_hit and (price_hit or spread_hit or pb_hit)

    sell_criteria = []
    if cfg.get("sell_trailing_drawdown_pct") is not None:
        gain_pct = None
        if close is not None and recent_avg is not None and recent_avg > 0:
            gain_pct = (close - recent_avg) / recent_avg * 100
        sell_criteria.append(
            make_criterion(
                "移动止盈",
                trail_hit,
                (
                    f"浮盈 {gain_pct:.0f}%（需≥{cfg['sell_min_unrealized_gain_pct']*100:.0f}%）"
                    f"，峰值回撤≥{cfg['sell_trailing_drawdown_pct']*100:.0f}%"
                    if gain_pct is not None
                    else "—"
                ),
                "浮盈未达移动止盈门槛或峰值回撤不足",
                applicable=recent_avg is not None and peak_price is not None,
            )
        )
    sell_criteria.extend([
        make_criterion(
            "PE 分位",
            pe_hit,
            f"{pct_text(pe_pct)}（需≥{cfg['sell_pe_percentile_min']:.0f}%）",
            "市盈率尚未修复至历史中高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "股债利差分位",
            spread_hit,
            f"{pct_text(spread_pct)}（需≤{cfg['sell_spread_percentile_max']:.0f}%）",
            "股债优势尚未明显收敛",
            applicable=spread_pct is not None,
        ),
    ])
    price_criterion = make_sell_price_position_criterion(
        pct_above_low,
        cfg["sell_max_above_low_pct"],
        lookback,
        close=snapshot.get("close"),
        lookback_low=snapshot.get("lookback_low_price"),
    )
    if price_criterion is not None:
        sell_criteria.append(price_criterion)
    if pb_pct is not None and cfg["sell_pb_percentile_min"] < 95:
        sell_criteria.append(
            make_criterion(
                "PB 分位",
                pb_hit,
                f"{pct_text(pb_pct)}（需≥{cfg['sell_pb_percentile_min']:.0f}%）",
                "市净率尚未修复至历史中高位",
                applicable=True,
            )
        )
    if min_range is not None and year_range is not None:
        sell_criteria.append(
            make_criterion(
                "近1年区间高位",
                range_hit,
                f"{pct_text(year_range * 100)}（需≥{min_range * 100:.0f}%）",
                "价格尚未进入近1年高位区间",
                applicable=True,
            )
        )
    if combo_pe_min is not None and pe_pct is not None:
        sell_criteria.append(
            make_criterion(
                "PE 组合门槛",
                combo_pe_hit,
                f"{pct_text(pe_pct)}（组合需≥{combo_pe_min:.0f}%）",
                "PE 未达区间高位组合门槛",
                applicable=True,
            )
        )

    is_sell = trail_hit or val_hit

    reasons = []
    if trail_hit:
        reasons.append("移动止盈触发")
    if val_hit and not trail_hit:
        if pe_hit and (spread_hit or price_hit or pb_hit):
            if pe_hit:
                reasons.append("PE分位过高")
            if spread_hit:
                reasons.append("利差分位过低")
            if price_hit:
                close_val = snapshot.get("close")
                low = snapshot.get("lookback_low_price")
                sell_pct = cfg["sell_max_above_low_pct"]
                if close_val is not None and low is not None and sell_pct is not None:
                    min_close = low * (1 + sell_pct)
                    reasons.append(
                        f"收盘 {format_index_price(close_val)} 高于卖出线 {format_index_price(min_close)}"
                    )
                else:
                    reasons.append(f"距{lookback}日低点涨幅过大")
            if pb_hit and cfg["sell_pb_percentile_min"] < 95:
                reasons.append("PB分位过高")
        elif combo_hit:
            reasons.append("近1年区间高位且估值/价格组合触发")

    return {
        "is_sell": is_sell,
        "sell_criteria": [c for c in sell_criteria if c["applicable"]],
        "sell_summary": "触发波段卖出: " + "、".join(reasons) if is_sell else None,
    }


def evaluate_cn_broad_buy(snapshot, *, buy_only=False):
    """股债利差 + PE 分位 + 价格位置；买入需多数指标 favorable。"""
    index_code = snapshot.get("code")
    cfg = get_cn_broad_signal_config(index_code)

    pe_pct = snapshot.get("pe_percentile")
    pb_pct = snapshot.get("pb_percentile")
    spread_pct = snapshot.get("spread_percentile")
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(
        year_range, cfg.get("buy_near_year_low_range_pct")
    )
    spread_min = cfg["buy_spread_percentile_min"]
    pe_max = cfg["buy_pe_percentile_max"]
    if near_low:
        spread_min = max(
            0.0,
            spread_min - cfg.get("buy_near_year_low_spread_relax", 0),
        )
        pe_max = min(
            100.0,
            pe_max + cfg.get("buy_near_year_low_pe_relax", 0),
        )
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

    spread_ok = (
        spread_pct is not None
        and spread_pct >= spread_min
    )
    pe_ok = pe_pct is not None and pe_pct <= pe_max
    pb_ok = pb_pct is not None and pb_pct <= cfg["buy_pb_percentile_max"]

    criteria = [
        make_criterion(
            "股债利差分位",
            spread_ok,
            f"{pct_text(spread_pct)}（需≥{spread_min:.0f}%）",
            "股息率相对国债优势不足",
            applicable=spread_pct is not None,
        ),
        make_criterion(
            "PE 分位",
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{pe_max:.0f}%）",
            "市盈率处于历史中高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PB 分位",
            pb_ok,
            f"{pct_text(pb_pct)}（需≤{cfg['buy_pb_percentile_max']:.0f}%）",
            "市净率处于历史中高位",
            applicable=pb_pct is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        max_above_low,
        cfg["buy_low_lookback_days"],
        close=snapshot.get("close"),
        lookback_low=snapshot.get("lookback_low_price"),
    )
    if price_criterion is not None:
        criteria.append(price_criterion)
    drawdown_criterion = make_drawdown_from_high_criterion(
        snapshot.get("pct_below_high"),
        min_drawdown,
        cfg.get("buy_high_lookback_days", 252),
        close=snapshot.get("close"),
        lookback_high=snapshot.get("lookback_high_price"),
    )
    if drawdown_criterion is not None:
        criteria.append(drawdown_criterion)
    year_range_criterion = make_year_range_criterion(
        year_range,
        cfg.get("buy_max_year_range_pct"),
        cfg.get("buy_range_lookback_days", 252),
        close=snapshot.get("close"),
        range_low=snapshot.get("range_low_price"),
        range_high=snapshot.get("range_high_price"),
    )
    if year_range_criterion is not None:
        criteria.append(year_range_criterion)
    trend_criterion = make_trend_criterion(
        snapshot.get("ma_slope_pct"),
        year_range,
        cfg.get("buy_trend_min_ma_slope_pct"),
        cfg.get("buy_trend_downtrend_max_range_pct"),
        cfg.get("buy_trend_ma_days"),
        cfg.get("buy_trend_slope_lookback_days"),
    )
    if trend_criterion is not None:
        criteria.append(trend_criterion)

    applicable = [c for c in criteria if c["applicable"]]
    score = sum(1 for c in applicable if c["passed"])
    total = len(applicable)
    if total >= 3:
        need = max(cfg["buy_min_pass_score_floor"], total - 1)
    else:
        need = max(1, total)
    base_buy = (
        (spread_ok or not cfg["buy_require_spread"])
        and score >= need
        and total >= cfg["buy_min_applicable_criteria"]
    )
    is_buy = base_buy and price_position_ok(
        snapshot.get("pct_above_low"), max_above_low
    ) and drawdown_from_high_ok(
        snapshot.get("pct_below_high"),
        min_drawdown,
    ) and year_range_ok(year_range, cfg.get("buy_max_year_range_pct")    ) and trend_filter_ok(
        snapshot.get("ma_slope_pct"),
        year_range,
        cfg.get("buy_trend_min_ma_slope_pct"),
        cfg.get("buy_trend_downtrend_max_range_pct"),
    )

    if is_buy and not buy_only:
        from config import SELL_REBUY_GATE_ENABLED, SELL_REBUY_MAX_GAIN_PCT
        from sell_trailing import rebuy_allowed_after_take_profit

        stages = cfg.get("sell_stages") or []
        first_stage = float(stages[0]["gain_pct"]) if stages else float(
            cfg.get("sell_min_unrealized_gain_pct") or 0.50
        )
        if not rebuy_allowed_after_take_profit(
            close=snapshot.get("close"),
            cost_basis=snapshot.get("recent_signal_buy_avg"),
            peak_price=snapshot.get("peak_since_last_buy"),
            max_gain_pct=SELL_REBUY_MAX_GAIN_PCT,
            first_stage_gain_pct=first_stage,
            gate_enabled=SELL_REBUY_GATE_ENABLED,
        ):
            is_buy = False

    if buy_only:
        return {"is_buy": is_buy}

    sell_eval = evaluate_cn_broad_sell(snapshot)
    is_sell = sell_eval["is_sell"] and not is_buy

    failed = [c["name"] for c in applicable if not c["passed"]]
    if is_buy:
        summary = "股债利差与 PE 分位均处历史有利区间"
    elif is_sell:
        summary = sell_eval["sell_summary"]
    elif not spread_ok:
        summary = "股债利差未达买入门槛，是主要制约因素"
    elif failed:
        summary = f"未达标项: {'、'.join(failed)}（需{need}/{total}项达标）"
    else:
        summary = "指标接近但未同时满足买入条件"

    display_criteria = sell_eval["sell_criteria"] if is_sell else applicable
    display_score = sum(1 for c in display_criteria if c["passed"])

    return {
        "is_buy": is_buy,
        "is_sell": is_sell,
        "score": display_score,
        "total": len(display_criteria),
        "signal_short": _resolve_cn_broad_signal_short(is_buy, is_sell),
        "criteria": display_criteria,
        "summary": summary,
        "drop_to_buy": None,
        "drop_to_buy_line": None,
    }


def vectorized_cn_broad_buy_mask(panel, index_code):
    """向量化批量判定买入信号（回测专用，与 evaluate_cn_broad_buy 逻辑一致）。"""
    if panel is None or panel.empty:
        return pd.Series(dtype=bool)

    def _col(name):
        if name not in panel.columns:
            return pd.Series(np.nan, index=panel.index, dtype=float)
        return pd.to_numeric(panel[name], errors="coerce")

    cfg = get_cn_broad_signal_config(index_code)
    pe_pct = _col("pe_percentile")
    pb_pct = _col("pb_percentile")
    spread_pct = _col("spread_percentile")
    year_range = _col("year_range_position")
    pct_above_low = _col("pct_above_low")
    pct_below_high = _col("pct_below_high")
    ma_slope_pct = _col("ma_slope_pct")

    near_thr = cfg.get("buy_near_year_low_range_pct", 0.15)
    near_low = year_range.notna() & (year_range <= near_thr)

    spread_min = cfg["buy_spread_percentile_min"]
    pe_max = cfg["buy_pe_percentile_max"]
    if cfg.get("buy_near_year_low_spread_relax", 0) or cfg.get("buy_near_year_low_pe_relax", 0):
        spread_min_arr = np.where(
            near_low,
            np.maximum(0.0, spread_min - cfg.get("buy_near_year_low_spread_relax", 0)),
            spread_min,
        )
        pe_max_arr = np.where(
            near_low,
            np.minimum(100.0, pe_max + cfg.get("buy_near_year_low_pe_relax", 0)),
            pe_max,
        )
    else:
        spread_min_arr = spread_min
        pe_max_arr = pe_max

    spread_ok = spread_pct.notna() & (spread_pct >= spread_min_arr)
    pe_ok = pe_pct.notna() & (pe_pct <= pe_max_arr)
    pb_ok = pb_pct.notna() & (pb_pct <= cfg["buy_pb_percentile_max"])

    max_above = cfg["buy_max_above_low_pct"]
    if max_above is not None:
        max_above_low = np.full(len(panel), max_above, dtype=float)
        near_relax = cfg.get("buy_near_year_low_above_low_relax", 0)
        if near_relax:
            max_above_low = np.where(near_low, max_above + near_relax, max_above_low)
        mid_thr = cfg.get("buy_mid_range_position_pct", 0.35)
        mid_cap = cfg.get("buy_mid_range_max_above_low_pct", 0.02)
        mid = year_range.notna() & (year_range > mid_thr)
        max_above_low = np.where(mid, np.minimum(max_above_low, mid_cap), max_above_low)
        price_ok = pct_above_low.notna() & (pct_above_low <= max_above_low)
        price_applicable = pct_above_low.notna()
    else:
        price_ok = pd.Series(True, index=panel.index)
        price_applicable = pd.Series(False, index=panel.index)

    min_dd = cfg.get("buy_min_drawdown_from_high_pct")
    if min_dd is not None:
        # 与 evaluate_cn_broad_buy 一致：第三参为 buy_near_year_low_drawdown_waive_pct
        dd_near_thr = cfg.get("buy_near_year_low_drawdown_waive_pct")
        waived = year_range.notna() & (year_range <= dd_near_thr)
        effective_dd = np.where(waived.to_numpy(), np.nan, min_dd)
        dd_ok = waived | (
            pct_below_high.notna() & (pct_below_high.to_numpy() >= effective_dd)
        )
        dd_applicable = (~waived) & pct_below_high.notna()
    else:
        dd_ok = pd.Series(True, index=panel.index)
        dd_applicable = pd.Series(False, index=panel.index)

    yr_max = cfg.get("buy_max_year_range_pct")
    if yr_max is not None:
        yr_ok = year_range.notna() & (year_range <= yr_max)
        yr_applicable = year_range.notna()
    else:
        yr_ok = pd.Series(True, index=panel.index)
        yr_applicable = pd.Series(False, index=panel.index)

    min_slope = cfg.get("buy_trend_min_ma_slope_pct", -0.025)
    downtrend_max = cfg.get("buy_trend_downtrend_max_range_pct", 0.12)
    slope_known = ma_slope_pct.notna()
    trend_ok = (~slope_known) | (ma_slope_pct >= min_slope) | (
        year_range.notna() & (year_range <= downtrend_max)
    )
    trend_applicable = slope_known

    criteria = [
        (spread_ok, spread_pct.notna()),
        (pe_ok, pe_pct.notna()),
        (pb_ok, pb_pct.notna()),
        (price_ok, price_applicable),
        (dd_ok, dd_applicable),
        (yr_ok, yr_applicable),
        (trend_ok, trend_applicable),
    ]
    applicable_count = sum(app.astype(int) for _, app in criteria)
    score = sum(ok.astype(int) for ok, _ in criteria)
    total = applicable_count
    need = np.where(
        total.to_numpy() >= 3,
        np.maximum(cfg["buy_min_pass_score_floor"], total.to_numpy() - 1),
        np.maximum(1, total.to_numpy()),
    )
    spread_gate = spread_ok if cfg["buy_require_spread"] else pd.Series(True, index=panel.index)
    base_buy = (
        spread_gate
        & (score.to_numpy() >= need)
        & (total.to_numpy() >= cfg["buy_min_applicable_criteria"])
    )
    return pd.Series(
        base_buy & price_ok.to_numpy() & dd_ok.to_numpy() & yr_ok.to_numpy() & trend_ok.to_numpy(),
        index=panel.index,
    )


def format_cn_broad_section(snapshot, buy_eval, module="cn_broad"):
    cfg = get_cn_broad_signal_config(snapshot["code"])
    year_range = snapshot.get("year_range_position")
    near_low = is_near_year_low(
        year_range, cfg.get("buy_near_year_low_range_pct")
    )
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
    price_ceilings = build_buy_price_ceilings(
        snapshot,
        max_above_low,
        min_drawdown,
        cfg.get("buy_max_year_range_pct"),
        low_lookback_days=cfg["buy_low_lookback_days"],
        high_lookback_days=cfg.get("buy_high_lookback_days", 252),
        range_lookback_days=cfg.get("buy_range_lookback_days", 252),
    )
    price_floors = build_sell_price_floors(
        snapshot,
        cfg["sell_max_above_low_pct"],
        lookback_days=cfg["buy_low_lookback_days"],
    )
    drop, rise_breaks = cn_broad_drop_to_buy(snapshot)
    drop_breaks = (
        cn_broad_sell_trigger(snapshot) if buy_eval.get("is_sell") else None
    )
    buy_line = format_buy_trigger_line(
        drop,
        is_buy=buy_eval.get("is_buy"),
        rise_breaks_pct=rise_breaks,
        close=snapshot.get("close"),
        price_ceilings=price_ceilings,
    )
    sell_line = format_sell_trigger_line(
        is_sell=buy_eval.get("is_sell"),
        drop_breaks_pct=drop_breaks,
        close=snapshot.get("close"),
        price_floors=price_floors,
    )
    buy_eval = {
        **buy_eval,
        "drop_to_buy": drop,
        "rise_breaks_buy": rise_breaks,
        "drop_to_buy_line": buy_line,
        "buy_trigger_line": buy_line,
        "sell_trigger_line": sell_line,
    }
    from signal_enrich import build_section_dict, enrich_signal_eval

    buy_eval = enrich_signal_eval(snapshot, buy_eval)
    buy_eval = enrich_signal_buy_amount(snapshot["code"], snapshot, buy_eval)
    bond = snapshot.get("bond_yield")
    from live_snapshot import format_live_meta_extra

    bond_extra = f"国债 {bond:.2%}" if bond is not None else None
    meta_extras = [x for x in (bond_extra, format_live_meta_extra(snapshot)) if x]
    meta_line = format_data_meta_line(
        snapshot.get("data_date") or snapshot.get("date"),
        snapshot.get("history_start"),
        snapshot.get("history_days"),
        extras=meta_extras or None,
    )
    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        meta_line,
    ]
    append_signal_block(lines, buy_eval, module)
    return build_section_dict(snapshot, buy_eval, lines)


def format_cn_broad_report(snapshot, section, title=None):
    return section["text"], section
