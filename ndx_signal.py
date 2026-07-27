"""纳斯达克 100 估值信号与报告格式化。"""

from config import (
    NDX_BUY_FORWARD_PE_PERCENTILE_MAX,
    NDX_BUY_LOW_LOOKBACK_DAYS,
    NDX_BUY_MAX_ABOVE_LOW_PCT,
    NDX_BUY_PEG_FORWARD_MAX,
    NDX_BUY_RATE_PERCENTILE_MAX,
    NDX_BUY_TRAILING_PE_PERCENTILE_MAX,
    NDX_EXPECTED_GROWTH,
    NDX_FALLBACK_EXPECTED_GROWTH,
    NDX_HIGH_GROWTH_PEG_BONUS,
    NDX_HIGH_GROWTH_THRESHOLD,
)
from drop_to_buy import ndx_drop_to_buy, format_drop_to_buy_line
from price_position import make_price_position_criterion, price_position_ok
from signal_format import (
    SIGNAL_BUY,
    SIGNAL_HOLD,
    append_signal_block,
    format_module_header,
    make_criterion,
    pct_text,
)


def compute_peg(pe, growth_rate):
    """PEG = PE / 预期年化盈利增速（百分比）。"""
    if pe is None or growth_rate is None or growth_rate <= 0:
        return None
    return pe / (growth_rate * 100)


def resolve_ndx_expected_growth(snapshot):
    """与 report / 回测共用的盈利增速选取顺序。"""
    explicit = snapshot.get("expected_growth")
    if explicit is not None:
        return explicit
    if NDX_EXPECTED_GROWTH is not None:
        return NDX_EXPECTED_GROWTH
    implied = snapshot.get("implied_growth")
    if implied is not None and implied > 0:
        return implied
    historical = snapshot.get("historical_growth")
    if historical is not None and historical > 0:
        return historical
    return NDX_FALLBACK_EXPECTED_GROWTH


def evaluate_ndx_signal(snapshot):
    """PE 分位 + Forward PEG + 利率环境 三维买入框架。"""
    expected_growth = resolve_ndx_expected_growth(snapshot)

    trailing_pe = snapshot.get("trailing_pe")
    forward_pe = snapshot.get("forward_pe")
    peg_forward = compute_peg(forward_pe, expected_growth)
    peg_hist = compute_peg(trailing_pe, snapshot.get("historical_growth"))

    trailing_pct = snapshot.get("trailing_pe_percentile")
    forward_pct = snapshot.get("forward_pe_percentile")
    rate_pct = snapshot.get("us10y_percentile")

    trailing_ok = (
        trailing_pct is not None
        and trailing_pct <= NDX_BUY_TRAILING_PE_PERCENTILE_MAX
    )
    forward_ok = (
        forward_pct is not None
        and forward_pct <= NDX_BUY_FORWARD_PE_PERCENTILE_MAX
    )
    if forward_pct is not None:
        pe_ok = forward_ok
        pe_label = "Forward PE 分位"
        pe_pct = forward_pct
        pe_threshold = NDX_BUY_FORWARD_PE_PERCENTILE_MAX
    elif trailing_pct is not None:
        pe_ok = trailing_ok
        pe_label = "TTM PE 分位"
        pe_pct = trailing_pct
        pe_threshold = NDX_BUY_TRAILING_PE_PERCENTILE_MAX
    else:
        pe_ok = False
        pe_label = "PE 分位"
        pe_pct = None
        pe_threshold = NDX_BUY_FORWARD_PE_PERCENTILE_MAX

    peg_forward_max = NDX_BUY_PEG_FORWARD_MAX
    if expected_growth is not None and expected_growth >= NDX_HIGH_GROWTH_THRESHOLD:
        peg_forward_max += NDX_HIGH_GROWTH_PEG_BONUS

    peg_fwd_ok = peg_forward is not None and peg_forward <= peg_forward_max
    rate_ok = rate_pct is not None and rate_pct <= NDX_BUY_RATE_PERCENTILE_MAX

    buy_criteria = [
        make_criterion(
            pe_label,
            pe_ok,
            f"{pct_text(pe_pct)}（需≤{pe_threshold:.0f}%）",
            "市盈率处于近10年偏高位",
            applicable=pe_pct is not None,
        ),
        make_criterion(
            "PEG(Forward)",
            peg_fwd_ok,
            (
                f"{peg_forward:.2f}（需≤{peg_forward_max:.1f}）"
                if peg_forward is not None
                else "—"
            ),
            "估值相对预期盈利增速偏贵",
            applicable=peg_forward is not None,
        ),
        make_criterion(
            "10Y 利率分位",
            rate_ok,
            f"{pct_text(rate_pct)}（需≤{NDX_BUY_RATE_PERCENTILE_MAX:.0f}%）",
            "无风险利率偏高，压制科技股估值",
            applicable=rate_pct is not None,
        ),
    ]
    price_criterion = make_price_position_criterion(
        snapshot.get("pct_above_low"),
        NDX_BUY_MAX_ABOVE_LOW_PCT,
        NDX_BUY_LOW_LOOKBACK_DAYS,
    )
    if price_criterion is not None:
        buy_criteria.append(price_criterion)

    score = sum(1 for c in buy_criteria if c["passed"])
    is_buy = (
        pe_ok
        and peg_fwd_ok
        and rate_ok
        and price_position_ok(snapshot.get("pct_above_low"), NDX_BUY_MAX_ABOVE_LOW_PCT)
    )

    if is_buy:
        signal_short = SIGNAL_BUY
        summary = "估值分位偏低、PEG 合理且利率环境友好"
    else:
        signal_short = SIGNAL_HOLD
        failed = [c["name"] for c in buy_criteria if c["applicable"] and not c["passed"]]
        summary = f"未达标项: {'、'.join(failed)}" if failed else "指标接近但未同时满足买入条件"

    return {
        "peg_forward": peg_forward,
        "peg_historical": peg_hist,
        "expected_growth": expected_growth,
        "historical_growth": snapshot.get("historical_growth"),
        "implied_growth": snapshot.get("implied_growth"),
        "is_buy": is_buy,
        "score": score,
        "signal_short": signal_short,
        "criteria": buy_criteria,
        "summary": summary,
    }


def is_ndx_buy(snapshot):
    """判断快照是否满足纳指买入条件（供回测等场景复用）。"""
    return evaluate_ndx_signal(snapshot)["is_buy"]


def format_ndx_section(snapshot, signal_eval):
    drop, rise_breaks = ndx_drop_to_buy(snapshot)
    signal_eval = {
        **signal_eval,
        "drop_to_buy": drop,
        "rise_breaks_buy": rise_breaks,
        "drop_to_buy_line": format_drop_to_buy_line(
            drop, is_buy=signal_eval.get("is_buy"), rise_breaks_pct=rise_breaks
        ),
    }
    peg_fwd = signal_eval.get("peg_forward")
    peg_fwd_text = f"{peg_fwd:.2f}" if peg_fwd is not None else "—"
    div = snapshot.get("dividend_yield")
    div_text = f"{div:.2%}" if div is not None else "—"
    us10y = snapshot.get("us10y")
    us10y_text = f"{us10y:.2%}" if us10y is not None else "—"

    lines = [
        f"{snapshot['code']} {snapshot['name']}",
        (
            f"Forward PE {snapshot.get('forward_pe', 0):.2f} "
            f"(分位 {pct_text(snapshot.get('forward_pe_percentile'))}) | "
            f"PEG(Forward) {peg_fwd_text}"
        ),
    ]

    if signal_eval.get("implied_growth") is not None:
        lines.append(f"隐含增速 {signal_eval['implied_growth']:.1%}")

    lines.append(
        f"10Y美债 {us10y_text} | 利率分位 {pct_text(snapshot.get('us10y_percentile'))} | "
        f"股息率(QQQ代理) {div_text}"
    )

    append_signal_block(lines, signal_eval, "ndx")
    return {
        "code": snapshot["code"],
        "name": snapshot["name"],
        "text": "\n".join(lines),
        "signal_short": signal_eval["signal_short"],
    }


def format_ndx_report(snapshot, section):
    hist_days = snapshot.get("daily_history_days", snapshot.get("history_days", 0))
    trailing_days = snapshot.get("trailing_history_days", 0)
    years = snapshot.get("history_years", 10)
    lines = format_module_header(
        "纳斯达克100 估值信号",
        (
            f"{snapshot['date']} | 日频样本约 {hist_days} 个交易日 | "
            f"TTM PE 日频约 {trailing_days} 日 | 近{years}年"
        ),
        "买入逻辑: Forward PE分位偏低 + PEG(Forward)≤阈值 + 10Y利率分位不高（三项须同时满足）",
    )
    lines.append(section["text"])
    return "\n".join(lines), section
