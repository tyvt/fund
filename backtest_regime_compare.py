"""对比：等额 vs 位置分配 vs 位置分配+牛熊动态调整。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime

from backtest_rotation import run_portfolio_rotation
from backtest_trade_signals import DEFAULT_START
from config import (
    BACKTEST_OUTPUT_DIR,
    MARKET_REGIME_ENABLED,
    format_backtest_amount_note,
    resolve_backtest_amounts,
)
from market_data import configure_stdout_utf8
from market_regime import (
    RegimeConfig,
    summarize_regime_series,
)

OUTPUT_STEM = "regime_compare"


@dataclass
class StrategyResult:
    key: str
    label: str
    rotation_return_pct: float | None
    rotation_xirr_pct: float | None
    hold_return_pct: float | None
    hold_xirr_pct: float | None
    rotation_new_money: float
    pool_reused: float
    rotation_sells: int
    wfa_win_rate: float | None
    wfa_windows: int
    note: str = ""


def _run_one(
    key: str,
    label: str,
    amounts,
    panels,
    start: str,
    end: str | None,
    *,
    regime: bool,
) -> StrategyResult:
    regime_config = RegimeConfig(
        enabled=regime,
        position_alloc=bool(amounts.get("position_alloc")),
    )

    rot = run_portfolio_rotation(
        start,
        end,
        amounts,
        panels,
        mode="rotation",
        record_daily=regime,
        regime_config=regime_config,
    )
    hold = run_portfolio_rotation(
        start,
        end,
        amounts,
        panels,
        mode="hold",
        use_pool=False,
        rotation_gate=False,
        record_daily=regime,
        regime_config=regime_config,
    )

    wfa_win = None
    wfa_n = 0

    edge = None
    if rot.return_pct is not None and hold.return_pct is not None:
        edge = rot.return_pct - hold.return_pct

    note = ""
    if edge is not None:
        note = f"轮动利差 {edge:+.1f}pct"
    if regime and regime_config.enabled:
        from market_regime import build_regime_by_day

        regime_by_day = build_regime_by_day(
            panels, start, end, config=regime_config
        )
        counts = summarize_regime_series(regime_by_day)
        note += (
            f"；牛/熊/震荡 {counts.get('bull', 0)}/"
            f"{counts.get('bear', 0)}/{counts.get('neutral', 0)} 日"
        )

    return StrategyResult(
        key=key,
        label=label,
        rotation_return_pct=rot.return_pct,
        rotation_xirr_pct=rot.xirr_pct,
        hold_return_pct=hold.return_pct,
        hold_xirr_pct=hold.xirr_pct,
        rotation_new_money=rot.total_new_money,
        pool_reused=rot.pool_reused,
        rotation_sells=rot.sell_count,
        wfa_win_rate=wfa_win,
        wfa_windows=wfa_n,
        note=note.strip("；"),
    )


def run_comparison(start: str, end: str | None, panels) -> list[StrategyResult]:
    from backtest_buy_signals import get_panels
    from buy_amount_ranking import _preload_ranking_panels

    panels = panels or get_panels()
    _preload_ranking_panels(panels)

    equal_amt = resolve_backtest_amounts(
        panels=panels,
        position_alloc_mode=False,
    )
    pos_amt = resolve_backtest_amounts(
        panels=panels,
        position_alloc_mode=True,
    )

    results = [
        _run_one(
            "equal",
            "等额基准（无位置分配）",
            equal_amt,
            panels,
            start,
            end,
            regime=False,
        ),
        _run_one(
            "position",
            "位置分配（默认）",
            pos_amt,
            panels,
            start,
            end,
            regime=False,
        ),
        _run_one(
            "position_regime",
            "位置分配 + 牛熊动态",
            pos_amt,
            panels,
            start,
            end,
            regime=True,
        ),
    ]
    return results


def _fmt_pct(v):
    return "—" if v is None else f"{v:+.1f}%"


def print_table(results: list[StrategyResult]):
    print("\n=== 策略对比（智能轮动 vs 全持有）===")
    print(
        f"{'策略':<22} {'轮动收益':>9} {'持有收益':>9} {'利差':>8} "
        f"{'轮动XIRR':>9} {'净投入':>10} {'卖出':>5}"
    )
    print("-" * 78)
    for r in results:
        edge = (
            (r.rotation_return_pct or 0) - (r.hold_return_pct or 0)
            if r.rotation_return_pct is not None and r.hold_return_pct is not None
            else None
        )
        print(
            f"{r.label:<22} {_fmt_pct(r.rotation_return_pct):>9} "
            f"{_fmt_pct(r.hold_return_pct):>9} {_fmt_pct(edge):>8} "
            f"{_fmt_pct(r.rotation_xirr_pct):>9} {r.rotation_new_money:>10.0f} "
            f"{r.rotation_sells:>5}"
        )


def format_markdown(results, start, end, amounts) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 位置分配与牛熊动态策略对比",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{start} 至 {end or '最新'}  ",
        f"> 买入金额（位置分配组）：{format_backtest_amount_note(amounts)}  ",
        "> 牛熊判定：中证1000 + 创业板指 + 纳斯达克100 的**年区间位置**与 **MA 斜率**（现有面板字段）  ",
        "> 牛市：少买、提高轮动估值门槛；熊市：多买、降低轮动门槛  ",
        "",
        "## 对比结果",
        "",
        "| 策略 | 轮动收益 | 持有收益 | 利差 | 轮动XIRR | 持有XIRR | 净投入 | 卖出 | 备注 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    best_rot = max(
        (r for r in results if r.rotation_return_pct is not None),
        key=lambda x: x.rotation_return_pct or -999,
        default=None,
    )
    for r in results:
        edge = None
        if r.rotation_return_pct is not None and r.hold_return_pct is not None:
            edge = r.rotation_return_pct - r.hold_return_pct
        mark = " **最优**" if r is best_rot else ""
        lines.append(
            f"| {r.label}{mark} | {_fmt_pct(r.rotation_return_pct)} | "
            f"{_fmt_pct(r.hold_return_pct)} | {_fmt_pct(edge)} | "
            f"{_fmt_pct(r.rotation_xirr_pct)} | {_fmt_pct(r.hold_xirr_pct)} | "
            f"{r.rotation_new_money:,.0f} | {r.rotation_sells} | {r.note} |"
        )

    lines.extend([
        "",
        "## 解读",
        "",
        "- **等额基准**：各指数相同单次金额（旧默认）",
        "- **位置分配**：更常在低位买入的指数，单次金额更高（`BUY_AMOUNT_POSITION_ALLOC_ENABLED`）",
        "- **牛熊动态**：在位置分配基础上，按市场状态调节买入力度与轮动门槛",
        "",
        "若「位置分配 + 牛熊」相对「仅位置分配」利差更高、WFA 胜率不降，可考虑开启 `MARKET_REGIME_ENABLED=true`。",
        "",
        "复现：`python backtest_regime_compare.py`",
        "",
    ])
    return "\n".join(lines)


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="位置分配与牛熊动态策略回测对比")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--no-tier", action="store_true")
    args = parser.parse_args(argv)

    from backtest_buy_signals import get_panels

    panels = get_panels()
    amounts = resolve_backtest_amounts(
        tier_enabled=not args.no_tier,
        panels=panels,
        position_alloc_mode=True,
    )

    try:
        results = run_comparison(args.start, args.end, panels)
    except Exception as exc:
        print(f"对比失败: {exc}")
        return 1

    print_table(results)
    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.md"
    path.write_text(
        format_markdown(results, args.start, args.end, amounts),
        encoding="utf-8",
    )
    json_path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.json"
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "market_regime_enabled_config": MARKET_REGIME_ENABLED,
                "results": [asdict(r) for r in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n报告已保存: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
