# -*- coding: utf-8 -*-
"""不同投资金额 × 持仓数 × 调仓周期 网格优化，生成最优配置表。"""

from __future__ import annotations

import argparse
import itertools
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from dividend_lowvol_rotation.backtest import (
    BacktestContext,
    default_start_years,
    prepare_backtest_context,
    run_backtest,
)
from dividend_lowvol_rotation.backtest_validate import (
    load_index_benchmark_nav,
)
from dividend_lowvol_rotation.config import (
    BACKTEST_OUTPUT_DIR,
    BACKTEST_PREFETCH_SIZE,
)
from dividend_lowvol_rotation import dynamic_params as _dp_module
from dividend_lowvol_rotation.strategy_params import StrategyParams
from market_data import configure_stdout_utf8

_bond_yield_cache: tuple[float | None, str | None] = (None, None)
_bond_yield_fetched = False


def _prefetch_bond_yield() -> None:
    global _bond_yield_cache, _bond_yield_fetched
    if _bond_yield_fetched:
        return
    try:
        from market_data import get_gov_bond_yield
        payload = get_gov_bond_yield()
        _bond_yield_cache = (float(payload["bond_yield"]) * 100.0, payload.get("data_date"))
    except Exception:
        _bond_yield_cache = (None, None)
    _bond_yield_fetched = True


def _patch_dynamic_params() -> None:
    _prefetch_bond_yield()
    _orig = _dp_module._fetch_bond_yield_pct

    def _cached():
        return _bond_yield_cache

    _dp_module._fetch_bond_yield_pct = _cached

CAPITAL_LEVELS = [30_000, 50_000, 80_000, 100_000, 120_000, 150_000, 200_000]
TOP_N_VALUES = [3, 5, 8, 10, 15]
REBALANCE_DAYS_VALUES = [5, 10, 15, 20, 25, 30, 40]
SELL_RANK_MULTIPLIER_VALUES = [1.3, 1.5, 1.8, 2.0, 2.3, 2.5, 3.0]

LOT_SIZE = 100


@dataclass
class TrialResult:
    capital: int
    top_n: int
    rebalance_days: int
    sell_rank_multiplier: float
    total_return_pct: float | None
    cagr_pct: float | None
    max_drawdown_pct: float | None
    sharpe: float | None
    final_nav: float | None
    trade_count: int
    index_return_pct: float | None
    edge_vs_index_pct: float | None


def _fmt_pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def run_single(
    ctx: BacktestContext,
    start: str,
    end: str,
    capital: float,
    top_n: int,
    rebalance_days: int,
    sell_rank_multiplier: float,
    index_nav: pd.DataFrame,
    prefetch_size: int,
) -> TrialResult:
    sell_rank = max(int(round(top_n * sell_rank_multiplier)), top_n + 1)
    sp = StrategyParams(
        top_n=top_n,
        sell_rank=sell_rank,
        sell_rank_multiplier=sell_rank_multiplier,
    )

    try:
        strat_nav, trades_df, _, _, meta, _ = run_backtest(
            start=start,
            end=end,
            top_n=top_n,
            rebalance_days=rebalance_days,
            initial_capital=capital,
            sell_rank=sell_rank,
            prefetch_size=prefetch_size,
            ctx=ctx,
            record_details=False,
            verbose=False,
            strategy_params=sp,
        )
    except Exception as e:
        print(f"    ✗ {capital/10000:.0f}万 top={top_n} rebal={rebalance_days} "
              f"sell_mult={sell_rank_multiplier}: {e}")
        return TrialResult(
            capital=int(capital), top_n=top_n, rebalance_days=rebalance_days,
            sell_rank_multiplier=sell_rank_multiplier,
            total_return_pct=None, cagr_pct=None, max_drawdown_pct=None,
            sharpe=None, final_nav=None, trade_count=0,
            index_return_pct=None, edge_vs_index_pct=None,
        )

    total_ret = meta.get("total_return_pct")
    cagr = meta.get("cagr_pct")
    max_dd = meta.get("max_drawdown_pct")
    sharpe = meta.get("sharpe")
    final_nav = meta.get("final_nav")
    trade_count = meta.get("trade_count", 0)

    index_ret = None
    edge = None
    if not index_nav.empty:
        index_ret_pct = float(index_nav["nav"].iloc[-1]) / float(index_nav["nav"].iloc[0]) - 1
        index_ret = index_ret_pct * 100
        if total_ret is not None:
            edge = total_ret - index_ret

    return TrialResult(
        capital=int(capital), top_n=top_n, rebalance_days=rebalance_days,
        sell_rank_multiplier=sell_rank_multiplier,
        total_return_pct=total_ret, cagr_pct=cagr, max_drawdown_pct=max_dd,
        sharpe=sharpe, final_nav=final_nav, trade_count=trade_count,
        index_return_pct=index_ret, edge_vs_index_pct=edge,
    )


def effective_position_size(capital: float, top_n: int) -> float:
    return capital / top_n


def min_buyable_shares(capital: float, top_n: int, avg_price: float = 15.0) -> int:
    per_stock = capital / top_n
    shares = int(per_stock / avg_price / LOT_SIZE) * LOT_SIZE
    return max(shares, 0)


def generate_report(results: list[TrialResult], meta: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _gen_single_mode(results_subset: list[TrialResult], mode_label: str) -> list[str]:
        lines = [
            f"### {mode_label}",
            "",
            "## 最优配置总表",
            "",
            "| 本金 | 持仓数 | 调仓周期(天) | 卖出倍数 | 总收益 | 年化CAGR | 最大回撤 | 夏普比率 | 超额(vs指数) |",
            "|------|--------|-------------|---------|--------|---------|---------|---------|------------|",
        ]

        best_per_capital: dict[int, TrialResult] = {}
        for r in results_subset:
            if r.total_return_pct is None:
                continue
            key = r.capital
            if key not in best_per_capital or (r.cagr_pct or -999) > (best_per_capital[key].cagr_pct or -999):
                best_per_capital[key] = r

        for cap in CAPITAL_LEVELS:
            r = best_per_capital.get(cap)
            if r is None:
                lines.append(f"| {cap/10000:.0f}万 | — | — | — | — | — | — | — | — |")
                continue
            lines.append(
                f"| {cap/10000:.0f}万 | {r.top_n} | {r.rebalance_days} "
                f"| {r.sell_rank_multiplier:.1f} "
                f"| {_fmt_pct(r.total_return_pct)} "
                f"| {_fmt_pct(r.cagr_pct)} "
                f"| {_fmt_pct(r.max_drawdown_pct)} "
                f"| {r.sharpe:.2f} "
                f"| {_fmt_pct(r.edge_vs_index_pct)} |"
            )

        lines.extend(["", "## 详细分析：每个资金级别的 Top-3 参数组合", ""])

        for cap in CAPITAL_LEVELS:
            cap_results = [r for r in results_subset if r.capital == cap and r.total_return_pct is not None]
            if not cap_results:
                continue
            cap_results.sort(key=lambda r: r.cagr_pct or -999, reverse=True)
            top3 = cap_results[:3]
            lines.append(f"#### {cap/10000:.0f}万本金")
            lines.append("")
            lines.append("| 排名 | 持仓数 | 调仓周期 | 卖出倍数 | 总收益 | CAGR | 最大回撤 | 夏普 | 超额 |")
            lines.append("|------|--------|---------|---------|--------|------|---------|------|------|")
            for i, r in enumerate(top3, 1):
                lines.append(
                    f"| {i} | {r.top_n} | {r.rebalance_days}天 "
                    f"| {r.sell_rank_multiplier:.1f} "
                    f"| {_fmt_pct(r.total_return_pct)} "
                    f"| {_fmt_pct(r.cagr_pct)} "
                    f"| {_fmt_pct(r.max_drawdown_pct)} "
                    f"| {r.sharpe:.2f} "
                    f"| {_fmt_pct(r.edge_vs_index_pct)} |"
                )
            lines.append("")
        return lines

    lines = [
        "# 红利低波轮动 — 不同投资金额最优配置表",
        "",
        f"> 生成时间：{now}",
        f"> 回测区间：{meta['start']} ~ {meta['end']}",
        f"> 验证段：{meta.get('valid_start', 'N/A')} ~ {meta['end']}",
        "",
        "## 说明",
        "",
        "- 每个投资金额级别，从网格搜索中选出**年化收益率（CAGR）最高**的参数组合",
        "- A股最小交易单位为100股（1手），低资金时单只持仓金额不足会影响实际可买股数",
        "- 超额收益 = 策略收益 - 指数收益（H30269）",
        "",
    ]

    lines.extend(_gen_single_mode(results, "固定周期"))

    lines.extend([
        "",
        "## 资金量与持仓数关系分析",
        "",
        "| 本金 | 每只均配金额 | 建议持仓数 | 说明 |",
        "|------|------------|-----------|------|",
    ])

    best_per_capital: dict[int, TrialResult] = {}
    for r in results:
        if r.total_return_pct is None:
            continue
        key = r.capital
        if key not in best_per_capital or (r.cagr_pct or -999) > (best_per_capital[key].cagr_pct or -999):
            best_per_capital[key] = r

    for cap in CAPITAL_LEVELS:
        r = best_per_capital.get(cap)
        if r is None:
            continue
        per_stock = cap / r.top_n
        note = ""
        if per_stock < 3000:
            note = "单只持仓极小，建议减少持仓数"
        elif per_stock < 5000:
            note = "单只持仓偏小，注意流动性"
        elif per_stock < 10000:
            note = "适中"
        elif per_stock < 15000:
            note = "较好，可充分分散"
        else:
            note = "充裕，分散充分"
        lines.append(
            f"| {cap/10000:.0f}万 | ¥{per_stock:,.0f} | {r.top_n} | {note} |"
        )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="红利低波轮动：不同投资金额最优配置")
    parser.add_argument("--years", type=int, default=5, help="回测年数")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--valid-start", default=None, help="验证段起点（默认回测后半段）")
    parser.add_argument("--prefetch", type=int, default=BACKTEST_PREFETCH_SIZE)
    parser.add_argument("--trials", type=int, default=0, help=">0 时仅随机抽样N组（加速调试）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("-o", "--output-dir", default=None)
    args = parser.parse_args(argv)

    start = args.start or default_start_years(args.years)
    end = args.end or pd.Timestamp.today().strftime("%Y-%m-%d")

    if args.valid_start is None:
        ts_start = pd.Timestamp(start)
        ts_end = pd.Timestamp(end)
        mid = ts_start + (ts_end - ts_start) * 0.6
        valid_start = mid.strftime("%Y-%m-%d")
    else:
        valid_start = args.valid_start

    out_dir = Path(args.output_dir) if args.output_dir else BACKTEST_OUTPUT_DIR

    _patch_dynamic_params()

    total_combos = (
        len(CAPITAL_LEVELS) * len(TOP_N_VALUES)
        * len(REBALANCE_DAYS_VALUES) * len(SELL_RANK_MULTIPLIER_VALUES)
    )
    print(f"回测区间：{start} ~ {end}")
    print(f"验证段：{valid_start} ~ {end}")
    print(f"投资金额级别：{len(CAPITAL_LEVELS)} 个")
    print(f"参数组合总数：{total_combos}")

    t0 = time.time()
    print(f"\n预加载数据…")
    ctx = prepare_backtest_context(
        start, end,
        prefetch_size=args.prefetch,
        rebalance_days=10,
        verbose=True,
    )
    index_nav, _ = load_index_benchmark_nav("H30269", start, end, 100_000)

    keys = ["capital", "top_n", "rebalance_days", "sell_rank_multiplier"]
    combos = list(itertools.product(
        CAPITAL_LEVELS, TOP_N_VALUES, REBALANCE_DAYS_VALUES, SELL_RANK_MULTIPLIER_VALUES,
    ))

    if args.trials > 0 and args.trials < len(combos):
        import random
        rng = random.Random(args.seed)
        combos = rng.sample(combos, args.trials)
        print(f"随机抽样 {args.trials} 组参数进行测试")

    results: list[TrialResult] = []
    for i, combo in enumerate(combos, 1):
        cap, tn, rb, sm = combo
        print(
            f"\r[{i}/{len(combos)}] {cap/10000:.0f}万 top={tn} "
            f"rebal={rb} sell_mult={sm}           ",
            end="", flush=True,
        )
        r = run_single(
            ctx=ctx, start=start, end=end,
            capital=float(cap), top_n=tn,
            rebalance_days=rb, sell_rank_multiplier=sm,
            index_nav=index_nav, prefetch_size=args.prefetch,
        )
        results.append(r)
    print()

    print(f"\n\n回测完成，总耗时 {time.time() - t0:.0f} 秒")

    meta = {
        "start": start,
        "end": end,
        "valid_start": valid_start,
        "elapsed_sec": time.time() - t0,
        "total_combos": len(combos),
    }

    report = generate_report(results, meta)
    print("\n" + report)

    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "capital_optimizer_report.md"
    json_path = out_dir / "capital_optimizer_results.json"

    md_path.write_text(report, encoding="utf-8")

    json_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "meta": meta,
        "results": [asdict(r) for r in results],
    }
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n报告：{md_path}")
    print(f"数据：{json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
