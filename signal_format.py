"""各模块统一的信号、条件与操作建议输出格式。"""

SIGNAL_BUY = "买入"
SIGNAL_HOLD = "观望"
SIGNAL_OVERVALUED = "高估"
SIGNAL_SELL = "波段卖出"
SIGNAL_NO_DATA = "数据不全"

MARK_PASS = "✓"
MARK_FAIL = "✗"


def pct_text(value):
    return f"{value:.1f}%" if value is not None else "—"


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
    hints = {
        (SIGNAL_BUY, "dividend"): "可考虑分批买入红利指数；注意分散、控制仓位。",
        (SIGNAL_BUY, "a500"): "可考虑分批布局中证 A500；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "hs300"): "可考虑分批布局沪深300；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "zz500"): "可考虑分批布局中证500；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "zz1000"): "可考虑分批布局中证1000；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "kc50"): "可考虑分批布局科创50；股债利差与估值具吸引力。",
        (SIGNAL_BUY, "cyb"): "可考虑分批买入创业板；估值与 PEG 处于历史偏低区间。",
        (SIGNAL_BUY, "hstech"): "可考虑分批买入恒生科技；PE 与 PEG 处于历史偏低区间。",
        (SIGNAL_BUY, "ndx"): "可考虑分批买入纳指 100；估值、PEG 与利率环境均配合。",
        (SIGNAL_BUY, "spx"): "可考虑分批买入标普 500；估值、PEG 与利率环境均配合。",
        (SIGNAL_HOLD, "dividend"): "暂不买入；等待利差扩大或 PE 分位回落。",
        (SIGNAL_HOLD, "a500"): "暂不买入；等待股债利差或估值指标进一步改善。",
        (SIGNAL_HOLD, "hs300"): "暂不买入；等待股债利差或估值指标进一步改善。",
        (SIGNAL_HOLD, "zz500"): "暂不买入；等待股债利差或估值指标进一步改善。",
        (SIGNAL_HOLD, "zz1000"): "暂不买入；等待股债利差或估值指标进一步改善。",
        (SIGNAL_HOLD, "kc50"): "暂不买入；等待股债利差或估值指标进一步改善。",
        (SIGNAL_HOLD, "cyb"): "暂不买入；等待 PE/PB 分位回落或 PEG 改善。",
        (SIGNAL_HOLD, "hstech"): "暂不买入；等待 PE 分位回落或 PEG 改善。",
        (SIGNAL_HOLD, "ndx"): "暂不买入；等待 PE 分位回落、PEG 下降或利率下行。",
        (SIGNAL_HOLD, "spx"): "暂不买入；等待 PE 分位回落、PEG 下降或利率下行。",
        (SIGNAL_SELL, "a500"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_SELL, "hs300"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_SELL, "zz500"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_SELL, "zz1000"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_SELL, "kc50"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_SELL, "cyb"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_SELL, "hstech"): "波段卖出条件触发；持仓者可考虑减仓或止盈。",
        (SIGNAL_NO_DATA, "dividend"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "a500"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "hs300"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "zz500"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "zz1000"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "kc50"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "cyb"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "hstech"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "ndx"): "数据缺失，本次不给出操作建议。",
        (SIGNAL_NO_DATA, "spx"): "数据缺失，本次不给出操作建议。",
    }
    return hints.get((signal, module), "请结合仓位与风险偏好自行决策。")


def append_signal_block(lines, signal_eval, module):
    """在指标行之后追加统一的信号、条件与建议块。"""
    signal = signal_eval.get("signal_short", SIGNAL_HOLD)
    criteria = [c for c in signal_eval.get("criteria", []) if c.get("applicable", True)]
    passed = sum(1 for c in criteria if c["passed"])
    total = len(criteria)

    if total:
        lines.append(f"信号: {signal} | 达标 {passed}/{total}")
    else:
        lines.append(f"信号: {signal}")

    drop_line = signal_eval.get("buy_trigger_line") or signal_eval.get("drop_to_buy_line")
    if drop_line:
        lines.append(drop_line)

    sell_line = signal_eval.get("sell_trigger_line")
    if sell_line:
        lines.append(sell_line)

    for item in criteria:
        mark = MARK_PASS if item["passed"] else MARK_FAIL
        lines.append(f"{mark} {item['name']}: {item['detail']}")
        if not item["passed"] and item.get("fail_reason"):
            lines.append(f"  → {item['fail_reason']}")

    summary = signal_eval.get("summary")
    if summary:
        lines.append(f"结论: {summary}")

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
