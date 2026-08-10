"""走步前向分析（WFA）：按时间窗口检验智能轮动样本外表现。

原理
----
策略参数固定（不优化），将全历史切分为多个**样本外（OOS）**窗口，
每窗口独立回测智能轮动 vs 全持有。若多数窗口轮动更优，说明优势
跨时期稳定，而非仅靠某一段行情。

默认按**自然年**切分（2015 至今每年一段）；也可选季度或半年滚动。

用法
----
    python backtest_wfa.py
    python backtest_wfa.py --freq quarter
    python backtest_wfa.py --start 2016-01-01 --rolling --train-months 24 --test-months 12
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime

import pandas as pd

from backtest_rotation import run_portfolio_rotation
from backtest_trade_signals import DEFAULT_START
from config import (
    BACKTEST_OUTPUT_DIR,
    ROTATION_MARGINAL_HURDLE_ANN_PCT,
    format_backtest_amount_note,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8

DEFAULT_FREQ = "year"
OUTPUT_STEM = "wfa_rotation"


@dataclass
class WfaWindowResult:
    label: str
    start: str
    end: str
    rotation_return_pct: float | None
    hold_return_pct: float | None
    rotation_xirr_pct: float | None
    hold_xirr_pct: float | None
    return_edge_pct: float | None
    xirr_edge_pct: float | None
    rotation_buys: int
    hold_buys: int
    rotation_sells: int
    rotation_new_money: float
    hold_new_money: float
    pool_reused: float
    note: str = ""


@dataclass
class WfaSummary:
    windows: int
    windows_with_buys: int
    rotation_wins_return: int
    rotation_wins_xirr: int
    mean_return_edge_pct: float | None
    median_return_edge_pct: float | None
    mean_xirr_edge_pct: float | None
    stitched_oos_return_pct: float | None
    stitched_hold_return_pct: float | None
    full_rotation_return_pct: float | None
    full_hold_return_pct: float | None


def _resolve_end(end: str | None) -> pd.Timestamp:
    if end:
        return pd.Timestamp(end)
    return pd.Timestamp.today().normalize()


def iter_annual_windows(start: str, end: str | None):
    start_ts = pd.Timestamp(start)
    end_ts = _resolve_end(end)
    for year in range(start_ts.year, end_ts.year + 1):
        w_start = max(start_ts, pd.Timestamp(f"{year}-01-01"))
        w_end = min(end_ts, pd.Timestamp(f"{year}-12-31"))
        if w_start > w_end:
            continue
        yield str(year), w_start.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")


def iter_period_windows(start: str, end: str | None, months: int):
    start_ts = pd.Timestamp(start)
    end_ts = _resolve_end(end)
    cursor = start_ts
    idx = 1
    while cursor <= end_ts:
        w_end = min(cursor + pd.DateOffset(months=months) - pd.Timedelta(days=1), end_ts)
        if cursor > w_end:
            break
        label = f"{cursor.strftime('%Y-%m')}_{w_end.strftime('%Y-%m')}"
        yield label, cursor.strftime("%Y-%m-%d"), w_end.strftime("%Y-%m-%d")
        cursor = w_end + pd.Timedelta(days=1)
        idx += 1


def iter_rolling_windows(
    start: str,
    end: str | None,
    train_months: int,
    test_months: int,
):
    """滚动走步：跳过训练期，仅输出测试期窗口（参数不优化，训练期仅作暖场说明）。"""
    start_ts = pd.Timestamp(start)
    end_ts = _resolve_end(end)
    cursor = start_ts
    idx = 1
    while True:
        train_end = cursor + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        test_start = train_end + pd.Timedelta(days=1)
        test_end = test_start + pd.DateOffset(months=test_months) - pd.Timedelta(days=1)
        if test_start > end_ts:
            break
        test_end = min(test_end, end_ts)
        label = f"fold{idx}_{test_start.strftime('%Y%m')}-{test_end.strftime('%Y%m')}"
        yield label, test_start.strftime("%Y-%m-%d"), test_end.strftime("%Y-%m-%d")
        cursor = test_start
        idx += 1


def iter_wfa_windows(
    start: str,
    end: str | None,
    *,
    freq: str = DEFAULT_FREQ,
    rolling: bool = False,
    train_months: int = 24,
    test_months: int = 12,
):
    if rolling:
        yield from iter_rolling_windows(start, end, train_months, test_months)
        return
    if freq == "year":
        yield from iter_annual_windows(start, end)
    elif freq == "half":
        yield from iter_period_windows(start, end, 6)
    elif freq == "quarter":
        yield from iter_period_windows(start, end, 3)
    else:
        raise ValueError(f"未知频率: {freq}")


def _edge(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def _window_metrics(
    history: list[dict],
    cashflows: list[tuple],
    w_start: str,
    w_end: str,
) -> dict | None:
    from backtest_metrics import xirr_annual_pct

    days_in = [h for h in history if w_start <= h["day"] <= w_end]
    if not days_in:
        return None

    before = [h for h in history if h["day"] < w_start]
    v0 = float(before[-1]["value"]) if before else 0.0
    v1 = float(days_in[-1]["value"])
    inflow = float(sum(h["new_money_day"] for h in days_in))
    buys = int(sum(h["buy_count_day"] for h in days_in))
    sells = int(sum(h["sell_count_day"] for h in days_in))

    denom = v0 + inflow
    ret = (v1 - v0 - inflow) / denom * 100 if denom > 0 else None

    start_dt = pd.Timestamp(w_start)
    end_dt = pd.Timestamp(days_in[-1]["day"])
    amounts_cf: list[float] = []
    dates_cf: list = []
    if v0 > 0:
        amounts_cf.append(-v0)
        dates_cf.append(start_dt)
    for dt, amt in cashflows:
        ts = pd.Timestamp(dt)
        if start_dt <= ts <= end_dt and amt < 0:
            amounts_cf.append(float(amt))
            dates_cf.append(ts)
    amounts_cf.append(v1)
    dates_cf.append(end_dt)
    xirr = xirr_annual_pct(amounts_cf, dates_cf)

    return {
        "return_pct": ret,
        "xirr_pct": xirr,
        "buys": buys,
        "sells": sells,
        "new_money": inflow,
        "start_value": v0,
        "end_value": v1,
    }


def _run_window_continuous(
    label: str,
    start: str,
    end: str,
    rot_result,
    hold_result,
) -> WfaWindowResult:
    rot = _window_metrics(rot_result.daily_history, rot_result.cashflows, start, end)
    hold = _window_metrics(hold_result.daily_history, hold_result.cashflows, start, end)
    note = ""
    if rot is None or hold is None:
        note = "无交易日"
    elif rot["buys"] == 0 and hold["buys"] == 0:
        note = "无买入信号"
    return WfaWindowResult(
        label=label,
        start=start,
        end=end,
        rotation_return_pct=None if rot is None else rot["return_pct"],
        hold_return_pct=None if hold is None else hold["return_pct"],
        rotation_xirr_pct=None if rot is None else rot["xirr_pct"],
        hold_xirr_pct=None if hold is None else hold["xirr_pct"],
        return_edge_pct=_edge(
            None if rot is None else rot["return_pct"],
            None if hold is None else hold["return_pct"],
        ),
        xirr_edge_pct=_edge(
            None if rot is None else rot["xirr_pct"],
            None if hold is None else hold["xirr_pct"],
        ),
        rotation_buys=0 if rot is None else rot["buys"],
        hold_buys=0 if hold is None else hold["buys"],
        rotation_sells=0 if rot is None else rot["sells"],
        rotation_new_money=0.0 if rot is None else rot["new_money"],
        hold_new_money=0.0 if hold is None else hold["new_money"],
        pool_reused=0.0,
        note=note,
    )


def _run_window(
    label: str,
    start: str,
    end: str,
    amounts,
    panels,
) -> WfaWindowResult:
    rot = run_portfolio_rotation(
        start, end, amounts, panels, mode="rotation"
    )
    hold = run_portfolio_rotation(
        start,
        end,
        amounts,
        panels,
        mode="hold",
        use_pool=False,
        rotation_gate=False,
    )
    note = ""
    if rot.buy_count == 0 and hold.buy_count == 0:
        note = "无买入信号"
    return WfaWindowResult(
        label=label,
        start=start,
        end=end,
        rotation_return_pct=rot.return_pct,
        hold_return_pct=hold.return_pct,
        rotation_xirr_pct=rot.xirr_pct,
        hold_xirr_pct=hold.xirr_pct,
        return_edge_pct=_edge(rot.return_pct, hold.return_pct),
        xirr_edge_pct=_edge(rot.xirr_pct, hold.xirr_pct),
        rotation_buys=rot.buy_count,
        hold_buys=hold.buy_count,
        rotation_sells=rot.sell_count,
        rotation_new_money=rot.total_new_money,
        hold_new_money=hold.total_new_money,
        pool_reused=rot.pool_reused,
        note=note,
    )


def _stitched_return(windows: list[WfaWindowResult], attr: str) -> float | None:
    growth = 1.0
    used = 0
    for w in windows:
        val = getattr(w, attr)
        if val is None or w.note == "无买入信号":
            continue
        growth *= 1.0 + val / 100.0
        used += 1
    if used == 0:
        return None
    return (growth - 1.0) * 100.0


def _summarize(windows: list[WfaWindowResult], full_rot, full_hold) -> WfaSummary:
    active = [w for w in windows if w.note != "无买入信号"]
    ret_edges = [w.return_edge_pct for w in active if w.return_edge_pct is not None]
    xirr_edges = [w.xirr_edge_pct for w in active if w.xirr_edge_pct is not None]
    rot_wins_ret = sum(
        1
        for w in active
        if w.return_edge_pct is not None and w.return_edge_pct > 0
    )
    rot_wins_xirr = sum(
        1
        for w in active
        if w.xirr_edge_pct is not None and w.xirr_edge_pct > 0
    )

    def _mean(vals):
        return sum(vals) / len(vals) if vals else None

    def _median(vals):
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        mid = n // 2
        if n % 2:
            return s[mid]
        return (s[mid - 1] + s[mid]) / 2

    return WfaSummary(
        windows=len(windows),
        windows_with_buys=len(active),
        rotation_wins_return=rot_wins_ret,
        rotation_wins_xirr=rot_wins_xirr,
        mean_return_edge_pct=_mean(ret_edges),
        median_return_edge_pct=_median(ret_edges),
        mean_xirr_edge_pct=_mean(xirr_edges),
        stitched_oos_return_pct=_stitched_return(active, "rotation_return_pct"),
        stitched_hold_return_pct=_stitched_return(active, "hold_return_pct"),
        full_rotation_return_pct=full_rot.return_pct,
        full_hold_return_pct=full_hold.return_pct,
    )


def run_wfa(
    start_date: str,
    end_date: str | None,
    amounts,
    panels,
    *,
    freq: str = DEFAULT_FREQ,
    rolling: bool = False,
    train_months: int = 24,
    test_months: int = 12,
    regime_config=None,
) -> tuple[list[WfaWindowResult], WfaSummary, dict]:
    from backtest_buy_signals import get_panels
    from buy_amount_ranking import _preload_ranking_panels

    panels = panels or get_panels()
    _preload_ranking_panels(panels)

    rot_full = run_portfolio_rotation(
        start_date,
        end_date,
        amounts,
        panels,
        mode="rotation",
        record_daily=True,
        regime_config=regime_config,
    )
    hold_full = run_portfolio_rotation(
        start_date,
        end_date,
        amounts,
        panels,
        mode="hold",
        use_pool=False,
        rotation_gate=False,
        record_daily=True,
        regime_config=regime_config,
    )

    windows: list[WfaWindowResult] = []
    for label, w_start, w_end in iter_wfa_windows(
        start_date,
        end_date,
        freq=freq,
        rolling=rolling,
        train_months=train_months,
        test_months=test_months,
    ):
        windows.append(
            _run_window_continuous(label, w_start, w_end, rot_full, hold_full)
        )

    summary = _summarize(windows, rot_full, hold_full)
    meta = {
        "start": start_date,
        "end": end_date or "最新",
        "freq": freq,
        "rolling": rolling,
        "train_months": train_months if rolling else None,
        "test_months": test_months if rolling else None,
        "hurdle": ROTATION_MARGINAL_HURDLE_ANN_PCT,
    }
    return windows, summary, meta


def _fmt_pct(v: float | None, digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _fmt_money(v: float) -> str:
    return f"{v:,.0f}"


def print_summary(windows: list[WfaWindowResult], summary: WfaSummary, meta: dict):
    mode = "滚动走步" if meta["rolling"] else f"按{meta['freq']}切分"
    print(
        f"\n=== WFA 智能轮动 {meta['start']} 至 {meta['end']} "
        f"（{mode}，门槛 {meta['hurdle']:.0f}% 年化）==="
    )
    print(
        f"{'窗口':<14} {'轮动收益':>9} {'持有收益':>9} {'利差':>8} "
        f"{'轮动XIRR':>9} {'持有XIRR':>9} {'买入':>5} {'卖出':>5}"
    )
    print("-" * 82)
    for w in windows:
        edge = _fmt_pct(w.return_edge_pct)
        if w.return_edge_pct is not None and w.return_edge_pct > 0:
            edge = edge + "*"
        note = f" ({w.note})" if w.note else ""
        print(
            f"{w.label:<14} {_fmt_pct(w.rotation_return_pct):>9} "
            f"{_fmt_pct(w.hold_return_pct):>9} {edge:>8} "
            f"{_fmt_pct(w.rotation_xirr_pct):>9} {_fmt_pct(w.hold_xirr_pct):>9} "
            f"{w.rotation_buys:>5} {w.rotation_sells:>5}{note}"
        )
    print("-" * 82)
    n = summary.windows_with_buys
    print(
        f"样本外窗口 {summary.windows} 个（有效 {n} 个）| "
        f"轮动胜率（收益率）{summary.rotation_wins_return}/{n} | "
        f"轮动胜率（XIRR）{summary.rotation_wins_xirr}/{n}"
    )
    print(
        f"平均利差 {_fmt_pct(summary.mean_return_edge_pct)} | "
        f"中位利差 {_fmt_pct(summary.median_return_edge_pct)} | "
        f"平均 XIRR 利差 {_fmt_pct(summary.mean_xirr_edge_pct)}"
    )
    print(
        f"拼接 OOS 轮动 {_fmt_pct(summary.stitched_oos_return_pct)} | "
        f"拼接 OOS 持有 {_fmt_pct(summary.stitched_hold_return_pct)}"
    )
    print(
        f"全区间对照 轮动 {_fmt_pct(summary.full_rotation_return_pct)} | "
        f"持有 {_fmt_pct(summary.full_hold_return_pct)}"
    )


def format_markdown(
    windows: list[WfaWindowResult],
    summary: WfaSummary,
    meta: dict,
    amounts,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if meta["rolling"]:
        window_desc = (
            f"滚动走步：训练 {meta['train_months']} 月（暖场，不优化参数）+ "
            f"测试 {meta['test_months']} 月"
        )
    elif meta["freq"] == "year":
        window_desc = "按自然年切分，在连续持仓上统计各年样本外收益（持仓可跨年）"
    elif meta["freq"] == "half":
        window_desc = "按半年切分，每段独立样本外回测（期初空仓）"
    else:
        window_desc = "按季度切分，每段独立样本外回测（期初空仓）"

    n = summary.windows_with_buys
    win_ret = summary.rotation_wins_return
    win_rate_line = (
        f"- 轮动胜率（收益率）：**{win_ret}** / {n}（{win_ret / n * 100:.0f}%）"
        if n
        else "- 轮动胜率（收益率）：**—**"
    )
    lines = [
        "# 走步前向分析（WFA）— 智能轮动",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} 至 {meta['end']}  ",
        f"> 窗口：{window_desc}  ",
        f"> 买入金额：{format_backtest_amount_note(amounts)}  ",
        f"> 轮动门槛：持仓年化 < **{meta['hurdle']:.0f}%** 且当日有其他指数买点  ",
        "",
        "## 方法说明",
        "",
        "策略参数**固定**（与 `config.py` 一致，不做样本内优化）。",
        "先自起点连续运行智能轮动/全持有，再在各 OOS 窗口内统计",
        "该段增量收益（持仓与资金池可跨年延续）：",
        "",
        "- **智能轮动**：共享资金池 + 轮动门控",
        "- **全持有**：同期买点买入、不卖出（对照）",
        "",
        "若多数 OOS 窗口轮动收益率高于持有，说明优势跨时期较稳定。",
        "",
        "## 汇总",
        "",
        f"- 有效窗口：**{n}** / {summary.windows}",
        win_rate_line,
        f"- 轮动胜率（XIRR）：**{summary.rotation_wins_xirr}** / {n}",
        f"- 平均收益率利差（轮动−持有）：**{_fmt_pct(summary.mean_return_edge_pct)}**",
        f"- 中位收益率利差：**{_fmt_pct(summary.median_return_edge_pct)}**",
        f"- 平均 XIRR 利差：**{_fmt_pct(summary.mean_xirr_edge_pct)}**",
        f"- 拼接 OOS 收益（各窗几何连接）：轮动 **{_fmt_pct(summary.stitched_oos_return_pct)}**，"
        f"持有 **{_fmt_pct(summary.stitched_hold_return_pct)}**",
        f"- 全区间对照：轮动 **{_fmt_pct(summary.full_rotation_return_pct)}**，"
        f"持有 **{_fmt_pct(summary.full_hold_return_pct)}**",
        "",
        "## 分窗口明细",
        "",
        "| 窗口 | 起止 | 轮动收益 | 持有收益 | 利差 | 轮动XIRR | 持有XIRR | "
        "轮动买入 | 卖出 | 池复用 | 备注 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]

    for w in windows:
        period = f"{w.start} ~ {w.end}"
        win = ""
        if w.return_edge_pct is not None and w.return_edge_pct > 0:
            win = "轮动更优"
        elif w.return_edge_pct is not None and w.return_edge_pct < 0:
            win = "持有更优"
        note = w.note or win
        lines.append(
            f"| {w.label} | {period} | {_fmt_pct(w.rotation_return_pct)} | "
            f"{_fmt_pct(w.hold_return_pct)} | {_fmt_pct(w.return_edge_pct)} | "
            f"{_fmt_pct(w.rotation_xirr_pct)} | {_fmt_pct(w.hold_xirr_pct)} | "
            f"{w.rotation_buys} | {w.rotation_sells} | {_fmt_money(w.pool_reused)} | "
            f"{note} |"
        )

    lines.extend([
        "",
        "## 解读",
        "",
        "| 指标 | 含义 |",
        "| --- | --- |",
        "| 轮动胜率 | OOS 窗口中轮动总收益率高于持有的比例 |",
        "| 利差 | 单窗口轮动收益率 − 持有收益率 |",
        "| 拼接 OOS | 各窗口收益率几何相乘（假设每段独立注资） |",
        "| 全区间对照 | 连续回测整段历史的轮动 vs 持有 |",
        "",
        "**注意**：WFA 在连续持仓上切片，与分段独立回测不同；",
        "全区间对照为同一次连续模拟的整段结果。",
        "",
        "复现：`python backtest_wfa.py`",
        "",
    ])
    return "\n".join(lines)


def save_results(
    windows: list[WfaWindowResult],
    summary: WfaSummary,
    meta: dict,
    amounts,
    stem: str = OUTPUT_STEM,
) -> tuple[str, str]:
    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = BACKTEST_OUTPUT_DIR / f"{stem}.md"
    json_path = BACKTEST_OUTPUT_DIR / f"{stem}.json"
    md_path.write_text(
        format_markdown(windows, summary, meta, amounts), encoding="utf-8"
    )
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "summary": asdict(summary),
        "windows": [asdict(w) for w in windows],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(md_path), str(json_path)


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(
        description="走步前向分析（WFA）：分段检验智能轮动样本外表现"
    )
    parser.add_argument("--start", default=DEFAULT_START, help="起始日期")
    parser.add_argument("--end", default=None, help="结束日期（默认最新）")
    parser.add_argument(
        "--freq",
        choices=["year", "half", "quarter"],
        default=DEFAULT_FREQ,
        help="OOS 切分频率（默认按年）",
    )
    parser.add_argument(
        "--rolling",
        action="store_true",
        help="滚动走步（跳过训练月数后，按测试月数滑动）",
    )
    parser.add_argument(
        "--train-months",
        type=int,
        default=24,
        help="滚动模式训练期月数（仅暖场，不优化参数，默认 24）",
    )
    parser.add_argument(
        "--test-months",
        type=int,
        default=12,
        help="滚动模式测试期月数（默认 12）",
    )
    parser.add_argument("--no-tier", action="store_true", help="禁用涨跌缩放")
    parser.add_argument("--output", default=None, help="输出文件名（不含扩展名）")
    args = parser.parse_args(argv)

    amounts = resolve_backtest_amounts(tier_enabled=not args.no_tier)
    from backtest_buy_signals import get_panels

    print("正在加载数据（仅首次较慢）...")
    panels = get_panels()

    try:
        windows, summary, meta = run_wfa(
            args.start,
            args.end,
            amounts,
            panels,
            freq=args.freq,
            rolling=args.rolling,
            train_months=args.train_months,
            test_months=args.test_months,
        )
    except Exception as exc:
        print(f"WFA 失败: {exc}")
        return 1

    if not windows:
        print("无可用窗口")
        return 1

    print_summary(windows, summary, meta)
    stem = args.output or OUTPUT_STEM
    md_path, json_path = save_results(windows, summary, meta, amounts, stem=stem)
    print(f"\n报告已保存:\n  {md_path}\n  {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
