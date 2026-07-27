"""创业板指估值信号与报告格式化（加权 PE/PB 为主）。"""

from config import (
    CYB_BUY_LOW_LOOKBACK_DAYS,
    CYB_BUY_MAX_ABOVE_LOW_PCT,
    CYB_BUY_PB_PERCENTILE_MAX,
    CYB_BUY_PE_PERCENTILE_MAX,
    CYB_BUY_PEG_HIST_MAX,
    CYB_HISTORICAL_GROWTH,
    CYB_SELL_COMBO_PB_PERCENTILE_MIN,
    CYB_SELL_COMBO_PE_PERCENTILE_MIN,
    CYB_SELL_PB_PERCENTILE_MIN,
    CYB_SELL_PE_PERCENTILE_MIN,
    CYB_SELL_PEG_HIST_MIN,
)
from drop_to_buy import cyb_drop_to_buy, format_drop_to_buy_line
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
    """PEG = PE / 净利润增速（百分比）。"""
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def evaluate_cyb_signal(snapshot):
    """加权估值为主：PE/PB 历史分位 + 保守 PEG（近5年增速）。"""
    pe = snapshot.get("pe")
    peg_historical = compute_peg(pe, CYB_HISTORICAL_GROWTH)

    pe_pct = snapshot.get("pe_percentile")
    pb_pct = snapshot.get("pb_percentile")

    pe_ok = pe_pct is not None and pe_pct <= CYB_BUY_PE_PERCENTILE_MAX
    pb_ok = pb_pct is not None and pb_pct <= CYB_BUY_PB_PERCENTILE_MAX
    peg_hist_ok = peg_historical is not None and peg_historical <= CYB_BUY_PEG_HIST_MAX

    buy_criteria = [
        make_criterion(
            "PE 分位(加权)",
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{CYB_BUY_PE_PERCENTILE_MAX:.0f}%）",
            "市盈率处于历史中高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PB 分位(加权)",
            pb_ok,
            f"{pct_text(pb_pct)}（需≤{CYB_BUY_PB_PERCENTILE_MAX:.0f}%）",
            "市净率处于历史中高位",
            applicable=pb_pct is not None,
        ),
        make_criterion(
            "PEG(近5年增速)",
            peg_hist_ok,
            (
                f"{peg_historical:.2f}（需≤{CYB_BUY_PEG_HIST_MAX:.1f}）"
                if peg_historical is not None
                else "—"
            ),
            "估值相对历史盈利增速偏贵",
            applicable=peg_historical is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        CYB_BUY_MAX_ABOVE_LOW_PCT,
        CYB_BUY_LOW_LOOKBACK_DAYS,
    )
    if price_criterion is not None:
        buy_criteria.append(price_criterion)

    criteria = [c for c in buy_criteria if c["applicable"]]
    score = sum(1 for c in criteria if c["passed"])
    is_buy = (
        pe_ok
        and pb_ok
        and peg_hist_ok
        and price_position_ok(snapshot.get("pct_above_low"), CYB_BUY_MAX_ABOVE_LOW_PCT)
    )

    is_sell = False
    sell_reasons = []
    pe_high = pe_pct is not None and pe_pct >= CYB_SELL_PE_PERCENTILE_MIN
    pb_high = pb_pct is not None and pb_pct >= CYB_SELL_PB_PERCENTILE_MIN
    peg_combo = (
        peg_historical is not None
        and peg_historical >= CYB_SELL_PEG_HIST_MIN
        and pe_pct is not None
        and pb_pct is not None
        and pe_pct >= CYB_SELL_COMBO_PE_PERCENTILE_MIN
        and pb_pct >= CYB_SELL_COMBO_PB_PERCENTILE_MIN
    )
    if pe_high and pb_high:
        is_sell = True
        sell_reasons.append(f"PE分位{pct_text(pe_pct)}、PB分位{pct_text(pb_pct)}均偏高")
    elif peg_combo:
        is_sell = True
        sell_reasons.append(f"PEG(5年){peg_historical:.2f}偏高且估值不低")

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "加权 PE/PB 处历史低位，且 PEG(5年) 可接受"
    elif is_sell:
        signal_short = SIGNAL_SELL
        summary = "触发波段卖出: " + "；".join(sell_reasons)
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in criteria if not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_historical": peg_historical,
        "historical_growth": CYB_HISTORICAL_GROWTH,
        "is_buy": is_buy,
        "is_sell": is_sell,
        "score": score,
        "signal_short": signal_short,
        "criteria": criteria,
        "summary": summary,
    }


def format_cyb_section(snapshot, signal_eval):
    drop, rise_breaks = cyb_drop_to_buy(snapshot)
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

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        (
            f"PE {snapshot['pe']:.2f} | PE分位 {pct_text(snapshot.get('pe_percentile'))} | "
            f"PB {snapshot['pb']:.2f} | PB分位 {pct_text(snapshot.get('pb_percentile'))}"
        ),
        f"PEG(5年) {peg_hist_text} | 近5年增速 {signal_eval['historical_growth']:.1%}",
    ]
    append_signal_block(lines, signal_eval, "cyb")
    return {
        "code": snapshot["code"],
        "name": snapshot["name"],
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_cyb_report(snapshot, section):
    hist_days = snapshot.get("history_days", 0)
    lines = format_module_header(
        "创业板指 估值信号",
        f"{snapshot['date']} | 历史样本约 {hist_days} 个交易日 | 主口径: 加权PE/PB(乐咕创业板)，PE按月发布并按指数价日度折算",
        "买入逻辑: PE/PB 加权分位偏低 + PEG(5年)≤阈值（三项须同时满足）；卖出: PE/PB 分位偏高或 PEG 过高",
    )
    lines.append(section["text"])
    return "\n".join(lines), section
