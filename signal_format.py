"""各模块统一的信号、条件与操作建议输出格式。"""

from __future__ import annotations

import pandas as pd

SIGNAL_BUY = "买入"
SIGNAL_ELIGIBLE = "可买"
SIGNAL_NEAR_BUY = "接近买入"
SIGNAL_HOLD = "观望"
SIGNAL_OVERVALUED = "高估"
SIGNAL_SELL = "波段卖出"
SIGNAL_NO_DATA = "数据不全"

STRENGTH_TIER_STRONG = "强"
STRENGTH_TIER_ELIGIBLE = "中"
STRENGTH_TIER_WATCH = "弱"


def strength_tier_label(strength: int | None) -> str:
    """将 0-100 强度映射为强/中/弱。"""
    if strength is None:
        return "—"
    if strength >= 60:
        return STRENGTH_TIER_STRONG
    if strength >= 40:
        return STRENGTH_TIER_ELIGIBLE
    return STRENGTH_TIER_WATCH

MARK_PASS = "✓"
MARK_FAIL = "✗"


def format_date_label(value) -> str:
    """统一日期展示：YYYY-MM-DD。"""
    if value is None:
        return "—"
    try:
        if pd.isna(value):
            return "—"
    except (TypeError, ValueError):
        pass
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def panel_history_meta(panel, date_col="date") -> dict:
    """从估值面板提取数据截至日、历史起点与样本天数。"""
    if panel is None or panel.empty or date_col not in panel.columns:
        return {"data_date": None, "history_start": None, "history_days": 0}
    dates = pd.to_datetime(panel[date_col], errors="coerce").dropna()
    if dates.empty:
        return {"data_date": None, "history_start": None, "history_days": 0}
    return {
        "data_date": dates.max().date(),
        "history_start": dates.min().date(),
        "history_days": int(len(dates)),
    }


def merge_history_meta(snapshot: dict, panel, date_col="date") -> dict:
    """将历史样本元数据写入 snapshot。"""
    meta = panel_history_meta(panel, date_col=date_col)
    snapshot.update(meta)
    if snapshot.get("date") is None and meta.get("data_date"):
        snapshot["date"] = meta["data_date"]
    return snapshot


def format_data_meta_line(
    data_date=None,
    history_start=None,
    history_days=None,
    *,
    extras: list[str] | None = None,
) -> str:
    """统一报告元信息行。"""
    parts: list[str] = []
    if data_date is not None:
        parts.append(f"数据截至 {format_date_label(data_date)}")
    if history_start is not None and data_date is not None:
        days = history_days if history_days is not None else 0
        parts.append(
            f"历史 {format_date_label(history_start)} 至 {format_date_label(data_date)}"
            f"（{days} 个交易日）"
        )
    elif history_days:
        parts.append(f"历史样本 {history_days} 个交易日")
    if extras:
        parts.extend(str(x) for x in extras if x)
    return " | ".join(parts) if parts else "—"


def log_fetch_start(name: str, code: str | None = None) -> None:
    label = f"{name} ({code})" if code else name
    print(f"正在拉取 {label} ...")


def log_fetch_done(
    name: str,
    *,
    code: str | None = None,
    data_date=None,
    history_start=None,
    history_days=None,
    extra: str | None = None,
) -> None:
    label = f"{name} ({code})" if code else name
    meta = format_data_meta_line(
        data_date,
        history_start,
        history_days,
        extras=[extra] if extra else None,
    )
    print(f"{label} 就绪 | {meta}")


def pct_text(value):
    """格式化分位/百分比；贴近整数边界时多留一位，避免 99.01 显示成 99.0 造成误判。"""
    if value is None:
        return "—"
    one = round(float(value), 1)
    # 例如 99.008 → 一位小数成 99.0，但实际未达 ≤99 的硬门槛
    if abs(float(value) - one) >= 0.005 and abs(one - round(one)) < 1e-9:
        return f"{float(value):.2f}%"
    return f"{float(value):.1f}%"


def make_criterion(name, passed, detail, fail_reason=None, applicable=True):
    """构造单条判定结果。"""
    return {
        "name": name,
        "passed": bool(passed),
        "detail": detail,
        "fail_reason": fail_reason,
        "applicable": applicable,
    }


def resolve_action_hint(signal, module):
    """按信号类型返回统一操作建议。"""
    generic_hold = "暂不买入；等待股债利差或估值指标进一步改善。"
    generic_eligible = "条件已达标但强度未达强买入；可观望或小仓位试探。"
    generic_near = "多数条件接近达标，可提前关注、准备资金。"
    generic_sell = "波段卖出条件触发；持仓者可考虑减仓或止盈。"
    generic_no_data = "数据缺失，本次不给出操作建议。"
    hints = {
        (SIGNAL_BUY, "dividend"): "可考虑分批买入红利指数；注意分散、控制仓位。",
        (SIGNAL_BUY, "zz1000"): "可考虑分批布局中证1000；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "kc50"): "可考虑分批布局科创50；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "cyb"): "可考虑分批买入创业板；估值处历史偏低区间。",
        (SIGNAL_BUY, "ndx"): "可考虑分批买入纳指 100；估值与利率环境配合。",
        (SIGNAL_BUY, "spx"): "可考虑分批买入标普 500；估值与利率环境配合。",
        (SIGNAL_ELIGIBLE, "dividend"): generic_eligible,
        (SIGNAL_ELIGIBLE, "zz1000"): generic_eligible,
        (SIGNAL_ELIGIBLE, "kc50"): generic_eligible,
        (SIGNAL_ELIGIBLE, "cyb"): generic_eligible,
        (SIGNAL_ELIGIBLE, "ndx"): generic_eligible,
        (SIGNAL_ELIGIBLE, "spx"): generic_eligible,
        (SIGNAL_NEAR_BUY, "dividend"): generic_near,
        (SIGNAL_NEAR_BUY, "zz1000"): generic_near,
        (SIGNAL_NEAR_BUY, "kc50"): generic_near,
        (SIGNAL_NEAR_BUY, "cyb"): generic_near,
        (SIGNAL_NEAR_BUY, "ndx"): generic_near,
        (SIGNAL_NEAR_BUY, "spx"): generic_near,
        (SIGNAL_HOLD, "dividend"): "暂不买入；等待利差或 PE 分位改善。",
        (SIGNAL_HOLD, "zz1000"): generic_hold,
        (SIGNAL_HOLD, "kc50"): generic_hold,
        (SIGNAL_HOLD, "cyb"): "暂不买入；等待 PE/PB 分位改善。",
        (SIGNAL_HOLD, "ndx"): "暂不买入；等待 PE 分位回落或利率下行。",
        (SIGNAL_HOLD, "spx"): "暂不买入；等待 PE 分位回落或利率下行。",
        (SIGNAL_SELL, "zz1000"): generic_sell,
        (SIGNAL_SELL, "kc50"): generic_sell,
        (SIGNAL_SELL, "cyb"): generic_sell,
        (SIGNAL_NO_DATA, "dividend"): generic_no_data,
        (SIGNAL_NO_DATA, "zz1000"): generic_no_data,
        (SIGNAL_NO_DATA, "kc50"): generic_no_data,
        (SIGNAL_NO_DATA, "cyb"): generic_no_data,
        (SIGNAL_NO_DATA, "ndx"): generic_no_data,
        (SIGNAL_NO_DATA, "spx"): generic_no_data,
    }
    return hints.get((signal, module), "请结合仓位与风险偏好自行决策。")


def append_signal_block(lines, signal_eval, module):
    """在指标行之后追加统一的信号、条件与建议块。"""
    signal = signal_eval.get("signal_short", SIGNAL_HOLD)
    criteria = [c for c in signal_eval.get("criteria", []) if c.get("applicable", True)]
    passed = sum(1 for c in criteria if c["passed"])
    total = len(criteria)

    if total:
        strength = signal_eval.get("signal_strength")
        tier = signal_eval.get("strength_tier")
        if strength is not None:
            tier_text = f" | 强度 {strength}/100（{tier}）" if tier else f" | 强度 {strength}/100"
            lines.append(f"信号: {signal} | 达标 {passed}/{total}{tier_text}")
        else:
            lines.append(f"信号: {signal} | 达标 {passed}/{total}")
    else:
        lines.append(f"信号: {signal}")

    if signal_eval.get("cooldown_note"):
        lines.append(signal_eval["cooldown_note"])
    if signal_eval.get("strength_note"):
        lines.append(signal_eval["strength_note"])

    drop_line = signal_eval.get("buy_trigger_line") or signal_eval.get("drop_to_buy_line")
    if drop_line and (signal_eval.get("is_buy") or signal_eval.get("criteria_met")):
        lines.append(drop_line)

    buy_amount_line = signal_eval.get("buy_amount_line")
    if buy_amount_line:
        lines.append(buy_amount_line)

    sell_line = signal_eval.get("sell_trigger_line")
    if sell_line and signal_eval.get("is_sell"):
        lines.append(sell_line)

    for item in criteria:
        mark = MARK_PASS if item["passed"] else MARK_FAIL
        lines.append(f"{mark} {item['name']}: {item['detail']}")

    hint = signal_eval.get("action_hint") or resolve_action_hint(signal, module)
    lines.append(f"建议: {hint}")
    return lines


def format_module_header(title, meta_line, source_line=None):
    """统一模块报告头。"""
    lines = [title, meta_line]
    if source_line:
        lines.append(source_line)
    lines.append("")
    lines.append("─" * 24)
    return lines


def join_index_sections(sections) -> str:
    """将各指数 section 拼接为扁平报告正文。"""
    parts = []
    for index, section in enumerate(sections):
        if index > 0:
            parts.append("")
            parts.append("─" * 24)
        parts.append(section["text"])
    return "\n".join(parts)


def section_has_trade_signal(section) -> bool:
    """当日是否触发可执行买入或波段卖出信号。"""
    return section.get("signal_short") in (SIGNAL_BUY, SIGNAL_SELL)


def filter_trade_signal_sections(sections):
    """仅保留触发买卖信号的指数 section。"""
    return [section for section in sections if section_has_trade_signal(section)]
