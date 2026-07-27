# -*- coding: utf-8 -*-
"""红利指数：信号生成与报告构建。"""

from config import get_dividend_signal_config, select_indices
from drop_to_buy import dividend_drop_to_buy, format_drop_to_buy_line
from dividend_data import (
    build_signal_history,
    collect_index_results,
    evaluate_buy_signal,
)
from market_data import get_gov_bond_yield, get_gov_bond_yield_history
from price_position import make_price_position_criterion
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_NO_DATA,
    append_signal_block,
    format_module_header,
    make_criterion,
    pct_text,
)


def build_dividend_signal_eval(index_code, buy_eval, spread, spread_pct, pe_pct):
    cfg = get_dividend_signal_config(index_code)
    pct_above_low = buy_eval.get("pct_above_low")
    max_above_low = cfg.get("buy_max_above_low_pct")
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
            pct_above_low, max_above_low, lookback
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
    drop, rise_breaks = dividend_drop_to_buy(index_code, bond_history)
    signal_eval["drop_to_buy"] = drop
    signal_eval["rise_breaks_buy"] = rise_breaks
    signal_eval["drop_to_buy_line"] = format_drop_to_buy_line(
        drop, is_buy=signal_eval.get("is_buy"), rise_breaks_pct=rise_breaks
    )

    lines = [
        f"{index_code} {index_name}",
        f"利差 {spread:.2%} | 利差分位 {pct_text(spread_pct)} | 股息率 {dividend_yield:.2%}",
        f"PE {pe:.2f} | PE分位 {pct_text(pe_pct)}",
    ]
    append_signal_block(lines, signal_eval, "dividend")
    return {
        "code": index_code,
        "name": index_name,
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_report_header(index_results, bond_yield, bond_date, bond_history):
    index_dates = [item["index_date"] for item in index_results if item.get("index_date")]
    index_date = max(index_dates) if index_dates else "-"
    bond_text = f"{bond_yield:.2%}" if bond_yield is not None else "—"
    return format_module_header(
        "红利指数",
        f"{index_date} | 国债 {bond_text}",
        "买入: 利差>阈值且利差分位高、PE分位低（各指数阈值见判定行）；展示值与判定均来自估值面板",
    )


def build_report(index_results, bond_yield, bond_date, bond_history=None):
    sections = [
        build_index_section(
            item["code"],
            item["name"],
            item["pe"],
            item["dividend_yield"],
            bond_yield,
            bond_history,
        )
        for item in index_results
    ]

    lines = format_report_header(
        index_results, bond_yield, bond_date, bond_history
    )

    for index, section in enumerate(sections):
        if index > 0:
            lines.append("")
            lines.append("─" * 24)
        lines.append(section["text"])

    return "\n".join(lines), sections


def generate_report(index_codes=None):
    """拉取数据并生成红利指数报告。"""
    indices = select_indices(index_codes)
    bond_yield, bond_date = get_gov_bond_yield()
    bond_history = get_gov_bond_yield_history()
    index_results = collect_index_results(indices, bond_history, bond_yield)
    return build_report(index_results, bond_yield, bond_date, bond_history)
