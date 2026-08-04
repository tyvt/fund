"""跨指数信号横向对比表。"""

from __future__ import annotations

from signal_format import pct_text


def _primary_pe_label(snapshot: dict) -> str:
    for key, label in (
        ("forward_pe_percentile", "FPE"),
        ("trailing_pe_percentile", "TTM"),
        ("pe_percentile", "PE"),
    ):
        val = snapshot.get(key)
        if val is not None:
            return f"{label} {pct_text(val)}"
    pb = snapshot.get("pb_percentile")
    if pb is not None:
        return f"PB {pct_text(pb)}"
    spread = snapshot.get("spread_percentile")
    if spread is not None:
        return f"利差 {pct_text(spread)}"
    return "—"


def _range_label(snapshot: dict) -> str:
    pos = snapshot.get("year_range_position")
    if pos is None:
        return "—"
    return pct_text(float(pos) * 100)


def format_signal_comparison_table(sections: list[dict]) -> str:
    """生成各指数信号强度横向对比 Markdown 表。"""
    rows = []
    for section in sections:
        code = section.get("code", "—")
        name = section.get("name", "—")
        signal = section.get("signal_short", "—")
        strength = section.get("signal_strength")
        strength_text = f"{strength}" if strength is not None else "—"
        tier = section.get("strength_tier") or "—"
        snapshot = section.get("snapshot") or {}
        pe_label = _primary_pe_label(snapshot)
        range_label = _range_label(snapshot)
        passed = section.get("score")
        total = section.get("total")
        score_text = (
            f"{passed}/{total}" if passed is not None and total is not None else "—"
        )
        criteria_met = section.get("criteria_met")
        gate_text = (
            "✅" if criteria_met
            else "❌" if criteria_met is not None
            else "—"
        )
        rows.append(
            (
                strength if strength is not None else -1,
                code,
                name,
                signal,
                strength_text,
                tier,
                gate_text,
                pe_label,
                range_label,
                score_text,
            )
        )

    if not rows:
        return ""

    rows.sort(key=lambda r: (-r[0], r[1]))
    lines = [
        "",
        "═" * 24,
        "跨指数信号对比（按强度降序）",
        "",
        "| 代码 | 名称 | 信号 | 强度 | 分级 | 硬门槛 | 估值 | 年区间 | 达标 |",
        "| --- | --- | --- | ---: | --- | --- | --- | ---: | ---: |",
    ]
    for (
        _,
        code,
        name,
        signal,
        strength_text,
        tier,
        gate_text,
        pe_label,
        range_label,
        score_text,
    ) in rows:
        lines.append(
            f"| {code} | {name} | {signal} | {strength_text} | {tier} | "
            f"{gate_text} | {pe_label} | {range_label} | {score_text} |"
        )
    lines.append("")
    lines.append(
        "说明：强度=估值(40)+价格位置(30)+趋势(20)+就绪度(10)；"
        "≥60 强买入、40-59 可买、<40 观望；"
        "硬门槛=各模块买入条件是否全部达标（✅/❌），强度分高但未达标仍为观望；"
        "年区间为近1年价格位置百分比。详见 README「信号强度评分方法论」。"
    )
    return "\n".join(lines)
