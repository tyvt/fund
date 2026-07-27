"""恒生科技指数估值信号与报告格式化（PE + PEG + 股息率分位，无 PB/PS 历史数据源）。"""

from config import (
    HSTECH_BUY_DIV_PERCENTILE_MIN,
    HSTECH_BUY_LOW_LOOKBACK_DAYS,
    HSTECH_BUY_MAX_ABOVE_LOW_PCT,
    HSTECH_BUY_PE_PERCENTILE_MAX,
    HSTECH_BUY_PEG_HIST_MAX,
    HSTECH_HISTORICAL_GROWTH,
    HSTECH_SELL_PE_PERCENTILE_MIN,
    HSTECH_SELL_PEG_HIST_MIN,
)
from drop_to_buy import format_drop_to_buy_line, hstech_drop_to_buy
from price_position import make_price_position_criterion, price_position_ok
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    SIGNAL_SELL,
    append_signal_block,
    format_module_header,
    make_criterion,
    pct_text,
)


def compute_peg(pe, growth_rate):
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def evaluate_hstech_signal(snapshot):
    """PE 历史分位 + 保守 PEG（近5年增速）+ 股息率分位；乐咕暂无 PB/PS 历史。"""
    pe = snapshot.get("pe")
    peg_historical = compute_peg(pe, HSTECH_HISTORICAL_GROWTH)
    pe_pct = snapshot.get("pe_percentile")
    div_pct = snapshot.get("dividend_percentile")

    pe_ok = pe_pct is not None and pe_pct <= HSTECH_BUY_PE_PERCENTILE_MAX
    peg_hist_ok = (
        peg_historical is not None and peg_historical <= HSTECH_BUY_PEG_HIST_MAX
    )
    div_ok = (
        div_pct is not None and div_pct >= HSTECH_BUY_DIV_PERCENTILE_MIN
    )

    buy_criteria = [
        make_criterion(
            "PE 分位",
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{HSTECH_BUY_PE_PERCENTILE_MAX:.0f}%）",
            "市盈率处于历史中高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PEG(近5年增速)",
            peg_hist_ok,
            (
                f"{peg_historical:.2f}（需≤{HSTECH_BUY_PEG_HIST_MAX:.1f}）"
                if peg_historical is not None
                else "—"
            ),
            "估值相对历史盈利增速偏贵",
            applicable=peg_historical is not None,
        ),
        make_criterion(
            "股息率分位",
            div_ok,
            f"{pct_text(div_pct)}（需≥{HSTECH_BUY_DIV_PERCENTILE_MIN:.0f}%）",
            "股息率相对历史偏低，估值吸引力不足",
            applicable=div_pct is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        HSTECH_BUY_MAX_ABOVE_LOW_PCT,
        HSTECH_BUY_LOW_LOOKBACK_DAYS,
    )
    if price_criterion is not None:
        buy_criteria.append(price_criterion)

    criteria = [c for c in buy_criteria if c["applicable"]]
    score = sum(1 for c in criteria if c["passed"])
    is_buy = (
        pe_ok
        and peg_hist_ok
        and div_ok
        and price_position_ok(
            snapshot.get("pct_above_low"), HSTECH_BUY_MAX_ABOVE_LOW_PCT
        )
    )

    is_sell = False
    sell_reasons = []
    pe_high = pe_pct is not None and pe_pct >= HSTECH_SELL_PE_PERCENTILE_MIN
    peg_high = (
        peg_historical is not None
        and peg_historical >= HSTECH_SELL_PEG_HIST_MIN
        and pe_pct is not None
        and pe_pct >= 60
    )
    if pe_high:
        is_sell = True
        sell_reasons.append(f"PE分位{pct_text(pe_pct)}偏高")
    elif peg_high:
        is_sell = True
        sell_reasons.append(f"PEG(5年){peg_historical:.2f}偏高且估值不低")

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "PE 处历史低位，PEG(5年) 可接受，且股息率相对历史偏高"
    elif is_sell:
        signal_short = SIGNAL_SELL
        summary = "触发波段卖出: " + "；".join(sell_reasons)
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in criteria if not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_historical": peg_historical,
        "historical_growth": HSTECH_HISTORICAL_GROWTH,
        "is_buy": is_buy,
        "is_sell": is_sell,
        "score": score,
        "signal_short": signal_short,
        "criteria": criteria,
        "summary": summary,
    }


def format_hstech_section(snapshot, signal_eval):
    drop, rise_breaks = hstech_drop_to_buy(snapshot)
    signal_eval = {
        **signal_eval,
        "drop_to_buy": drop,
        "rise_breaks_buy": rise_breaks,
        "drop_to_buy_line": format_drop_to_buy_line(
            drop, is_buy=signal_eval.get("is_buy"), rise_breaks_pct=rise_breaks
        ),
    }
    peg_hist = signal_eval.get("peg_historical")
    peg_hist_text = f"{peg_hist:.2f}" if peg_hist is not None else "—"
    div_yield = snapshot.get("dividend_yield")

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        (
            f"PE {snapshot['pe']:.2f} | PE分位 {pct_text(snapshot.get('pe_percentile'))} | "
            f"股息率 {div_yield * 100:.2f}% | 股息率分位 {pct_text(snapshot.get('dividend_percentile'))}"
        ),
        f"PEG(5年) {peg_hist_text} | 近5年增速 {signal_eval['historical_growth']:.1%}",
    ]
    append_signal_block(lines, signal_eval, "hstech")
    return {
        "code": snapshot["code"],
        "name": snapshot["name"],
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_hstech_report(snapshot, section):
    hist_days = snapshot.get("history_days", 0)
    lines = format_module_header(
        "恒生科技指数 估值信号",
        f"{snapshot['date']} | 历史样本约 {hist_days} 个交易日 | 主口径: 乐咕恒生科技 PE/股息率（月度发布并按指数价日度折算）",
        "买入逻辑: PE 分位偏低 + PEG(5年)≤阈值 + 股息率分位偏高 + 价格位置（须同时满足）；卖出: PE 分位偏高或 PEG 过高",
    )
    lines.append(section["text"])
    return "\n".join(lines), section
