# -*- coding: utf-8 -*-
"""红利指数：信号生成与报告构建。"""

from config import (
    DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN,
    get_dividend_signal_config,
    select_indices,
)
from drop_to_buy import dividend_drop_to_buy, format_buy_trigger_line
from dividend_data import (
    assess_spread_10y_level,
    build_signal_history,
    collect_index_results,
    evaluate_buy_signal,
    format_dividend_spread_10y_line,
)
from market_data import get_gov_bond_yield, get_gov_bond_yield_history
from price_position import (
    build_buy_price_ceilings,
    effective_drawdown_threshold,
    effective_max_above_low_pct,
    is_near_year_low,
    make_price_position_criterion,
)
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_NO_DATA,
    append_signal_block,
    format_data_meta_line,
    join_index_sections,
    log_fetch_done,
    make_criterion,
    pct_text,
)


def build_dividend_signal_eval(index_code, buy_eval, spread, spread_pct, pe_pct):
    cfg = get_dividend_signal_config(index_code)
    pct_above_low = buy_eval.get("pct_above_low")
    max_above_low = cfg.get("buy_max_above_low_pct")
    spread_10y_pct = buy_eval.get("spread_10y_percentile")
    spread_10y_ok, spread_10y_verdict = assess_spread_10y_level(spread_10y_pct)
    spread_pct_ok = (
        spread_pct is not None
        and spread_pct >= cfg["buy_spread_percentile_min"]
    )
    spread_abs_ok = spread is not None and spread > cfg["buy_spread_min"]
    pe_ok = pe_pct is not None and pe_pct <= cfg["buy_pe_percentile_max"]

    buy_criteria = [
        make_criterion(
            "股息-国债利差",
            spread_abs_ok,
            f"{spread:.2%}（需>{cfg['buy_spread_min']:.2%}）" if spread is not None else "—",
            "股息率相对国债优势不足",
            applicable=spread is not None,
        ),
        make_criterion(
            "利差历史分位",
            spread_pct_ok,
            f"{pct_text(spread_pct)}（需≥{cfg['buy_spread_percentile_min']:.0f}%）",
            "利差处于历史中低位",
            applicable=spread_pct is not None,
        ),
        make_criterion(
            "近10年利差分位",
            spread_10y_ok if spread_10y_ok is not None else True,
            (
                f"{pct_text(spread_10y_pct)}（偏高线≥{DIVIDEND_SPREAD_HIGH_PERCENTILE_MIN:.0f}%）"
                if spread_10y_pct is not None
                else "—"
            ),
            "股债利差处于近10年偏高位置",
            applicable=spread_10y_pct is not None,
        ),
        make_criterion(
            "PE 分位",
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{cfg['buy_pe_percentile_max']:.0f}%）",
            "市盈率处于历史中高位",
            applicable=pe_pct is not None,
        ),
    ]
    if max_above_low is not None:
        lookback = cfg.get("buy_low_lookback_days", 60)
        price_criterion = make_price_position_criterion(
            pct_above_low,
            max_above_low,
            lookback,
            close=buy_eval.get("close"),
            lookback_low=buy_eval.get("lookback_low_price"),
        )
        if price_criterion is not None:
            buy_criteria.append(price_criterion)

    is_buy = buy_eval.get("is_buy", False)
    signal_short = SIGNAL_BUY if is_buy else SIGNAL_HOLD

    if is_buy:
        summary = "股息率相对国债有足够优势，且估值处于历史偏低区间"
    else:
        failed = [c["name"] for c in buy_criteria if c["applicable"] and not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "未满足买入条件"

    return {
        "signal_short": signal_short,
        "criteria": buy_criteria,
        "summary": summary,
        "is_buy": is_buy,
    }


def build_index_section(
    index_code,
    index_name,
    pe,
    dividend_yield,
    bond_yield,
    bond_history=None,
    live_quotes=None,
):
    if not all([pe, dividend_yield, bond_yield]):
        no_data_eval = {
            "signal_short": SIGNAL_NO_DATA,
            "criteria": [],
            "summary": "PE、股息率或国债收益率缺失",
        }
        lines = [f"{index_code} {index_name}"]
        append_signal_block(lines, no_data_eval, "dividend")
        return {
            "code": index_code,
            "name": index_name,
            "text": "\n".join(lines),
            "signal_short": SIGNAL_NO_DATA,
        }

    buy_eval = evaluate_buy_signal(
        index_code, pe, dividend_yield, bond_yield, bond_history
    )
    buy_eval["code"] = index_code
    from live_snapshot import maybe_apply_live

    buy_eval = maybe_apply_live(buy_eval, live_quotes)

    panel_pe = buy_eval.get("pe")
    panel_div = buy_eval.get("dividend_yield")
    if panel_pe is not None:
        pe = panel_pe
    if panel_div is not None:
        dividend_yield = panel_div

    spread = buy_eval.get("spread")
    if spread is None:
        spread = dividend_yield - bond_yield

    spread_pct = buy_eval.get("spread_percentile")
    pe_pct = buy_eval.get("pe_percentile")

    signal_eval = build_dividend_signal_eval(
        index_code, buy_eval, spread, spread_pct, pe_pct
    )
    drop, rise_breaks = dividend_drop_to_buy(
        index_code, bond_history, panel=buy_eval.get("panel")
    )
    cfg = get_dividend_signal_config(index_code)
    year_range = buy_eval.get("year_range_position")
    max_above_low = effective_max_above_low_pct(
        cfg.get("buy_max_above_low_pct"),
        year_range,
        cfg.get("buy_near_year_low_range_pct"),
        cfg.get("buy_near_year_low_above_low_relax", 0),
        cfg.get("buy_mid_range_position_pct"),
        cfg.get("buy_mid_range_max_above_low_pct"),
    )
    min_drawdown = effective_drawdown_threshold(
        cfg.get("buy_min_drawdown_from_high_pct"),
        year_range,
        cfg.get("buy_near_year_low_range_pct"),
    )
    price_ceilings = build_buy_price_ceilings(
        buy_eval,
        max_above_low,
        min_drawdown,
        cfg.get("buy_max_year_range_pct"),
        low_lookback_days=cfg.get("buy_low_lookback_days", 60),
        high_lookback_days=cfg.get("buy_high_lookback_days", 252),
    )
    buy_line = format_buy_trigger_line(
        drop,
        is_buy=signal_eval.get("is_buy"),
        rise_breaks_pct=rise_breaks,
        close=buy_eval.get("close"),
        price_ceilings=price_ceilings,
    )
    signal_eval["drop_to_buy"] = drop
    signal_eval["rise_breaks_buy"] = rise_breaks
    signal_eval["drop_to_buy_line"] = buy_line
    signal_eval["buy_trigger_line"] = buy_line

    from buy_amount_config import enrich_signal_buy_amount
    from live_snapshot import format_live_meta_extra

    signal_eval = enrich_signal_buy_amount(index_code, buy_eval, signal_eval)

    bond_extra = f"国债 {bond_yield:.2%}" if bond_yield is not None else None
    meta_extras = [x for x in (bond_extra, format_live_meta_extra(buy_eval)) if x]
    meta_line = format_data_meta_line(
        buy_eval.get("data_date") or buy_eval.get("index_date"),
        buy_eval.get("history_start"),
        buy_eval.get("history_days"),
        extras=meta_extras or None,
    )
    lines = [
        f"{index_code} {index_name}",
        meta_line,
    ]
    append_signal_block(lines, signal_eval, "dividend")
    log_fetch_done(
        index_name,
        code=index_code,
        data_date=buy_eval.get("data_date") or buy_eval.get("index_date"),
        history_start=buy_eval.get("history_start"),
        history_days=buy_eval.get("history_days"),
    )
    return {
        "code": index_code,
        "name": index_name,
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
        "data_date": buy_eval.get("data_date") or buy_eval.get("index_date"),
        "history_start": buy_eval.get("history_start"),
        "history_days": buy_eval.get("history_days"),
    }


def build_report(index_results, bond_yield, bond_date, bond_history=None, live_quotes=None):
    sections = [
        build_index_section(
            item["code"],
            item["name"],
            item["pe"],
            item["dividend_yield"],
            bond_yield,
            bond_history,
            live_quotes=live_quotes,
        )
        for item in index_results
    ]

    for item, section in zip(index_results, sections):
        item.update(
            {
                k: section.get(k)
                for k in ("data_date", "history_start", "history_days")
                if k in section
            }
        )

    return join_index_sections(sections), sections


def generate_report(index_codes=None, live_quotes=None):
    """拉取数据并生成红利指数报告。"""
    indices = select_indices(index_codes)
    bond_yield, bond_date = get_gov_bond_yield()
    bond_history = get_gov_bond_yield_history()
    index_results = collect_index_results(indices, bond_history, bond_yield)
    return build_report(
        index_results, bond_yield, bond_date, bond_history, live_quotes=live_quotes
    )
