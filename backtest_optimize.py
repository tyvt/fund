"""买卖点阈值自动筛选与搜索（逐个验证 + 随机/网格/差分进化）。

默认 **screen**：对每个买卖指标在默认值附近试探，比较样本外轮动−持有利差变化。
影响低于阈值的参数标记为 drop，不参与后续搜索。

用法
----
    # 逐个验证全部买卖点指标（推荐先跑）
    python backtest_optimize.py --task screen

    # 仅验证买入或卖出类
    python backtest_optimize.py --task screen --side buy

    # 在「有影响」参数上随机搜索（需先 screen 或 --all-params）
    python backtest_optimize.py --task search --method random --trials 200

    # 对 screen 中 drop 的指标逐项关闭条件
    python backtest_optimize.py --task ablate

    # 全参数随机搜索（不筛）
    python backtest_optimize.py --task search --all-params --trials 300
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from backtest_rotation import run_portfolio_rotation
from backtest_trade_signals import DEFAULT_START
from backtest_wfa import run_wfa
from config import BACKTEST_OUTPUT_DIR, resolve_backtest_amounts
from market_data import configure_stdout_utf8
from signal_backtest_overlay import signal_backtest_overlay
from signal_param_catalog import (
    SignalParam,
    ablate_value,
    active_search_space,
    build_signal_param_catalog,
    catalog_by_id,
)

OUTPUT_STEM = "optimize_results"
ABLATE_STEM = "optimize_ablate"
DEFAULT_VALID_START = "2020-01-01"


@dataclass
class TrialResult:
    trial_id: int
    params: dict
    score: float
    full_edge_pct: float | None
    train_edge_pct: float | None
    valid_edge_pct: float | None
    rotation_return_pct: float | None
    hold_return_pct: float | None
    wfa_win_rate: float | None = None
    wfa_mean_edge_pct: float | None = None
    rotation_buys: int = 0
    rotation_sells: int = 0
    elapsed_sec: float = 0.0
    label: str = ""
    param_id: str | None = None
    param_value: float | None = None


@dataclass
class ScreenRow:
    param_id: str
    label: str
    side: str
    index: str | None
    default: float
    best_value: float
    best_valid_edge_pct: float | None
    baseline_valid_edge_pct: float | None
    edge_delta_pct: float | None
    verdict: str  # keep | drop
    sweep_results: list[dict] = field(default_factory=list)


@dataclass
class AblationRow:
    param_id: str
    label: str
    side: str
    index: str | None
    default: float
    ablated_value: float
    valid_edge_pct: float | None
    full_edge_pct: float | None
    edge_delta_pct: float | None
    rotation_buys: int
    rotation_sells: int
    buy_delta: int
    sell_delta: int
    signal_changed: bool
    verdict: str  # safe | minor | material


def _resolve_end(end: str | None) -> str:
    if end:
        return end
    return pd.Timestamp.today().strftime("%Y-%m-%d")


def _day_before(day: str) -> str:
    return (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def _period_edge(rot_result, hold_result, start: str, end: str) -> float | None:
    from backtest_wfa import _window_metrics

    if start > end:
        return None
    rot = _window_metrics(rot_result.daily_history, rot_result.cashflows, start, end)
    hold = _window_metrics(hold_result.daily_history, hold_result.cashflows, start, end)
    if rot is None or hold is None:
        return None
    if rot["return_pct"] is None or hold["return_pct"] is None:
        return None
    return rot["return_pct"] - hold["return_pct"]


def composite_score(
    train_edge: float | None,
    valid_edge: float | None,
    full_edge: float | None,
    wfa_win_rate: float | None = None,
    wfa_mean_edge: float | None = None,
) -> float:
    score = 0.0
    if valid_edge is not None:
        score += valid_edge * 0.50
        if valid_edge < 0:
            score -= 8.0
    if train_edge is not None:
        score += train_edge * 0.15
    if full_edge is not None:
        score += full_edge * 0.10
    if wfa_win_rate is not None:
        score += wfa_win_rate * 12.0
    if wfa_mean_edge is not None:
        score += wfa_mean_edge * 0.25
    return score


def _patches_from_trial_params(params: dict) -> dict:
    merged: dict[str, dict] = {}
    catalog = catalog_by_id()
    for pid, value in params.items():
        if pid == "position_alloc":
            continue
        spec = catalog.get(pid)
        if spec is None or spec.apply is None:
            continue
        patch = spec.apply(float(value))
        for kind, items in patch.items():
            merged.setdefault(kind, {}).update(items)
    return merged


def evaluate_trial(
    trial_id: int,
    params: dict,
    panels,
    start: str,
    end: str,
    valid_start: str,
    *,
    position_alloc: bool = True,
    run_wfa_flag: bool = False,
    label: str = "",
    param_id: str | None = None,
    param_value: float | None = None,
) -> TrialResult:
    t0 = time.perf_counter()
    patches = _patches_from_trial_params(params)
    amounts = resolve_backtest_amounts(
        panels=panels,
        position_alloc_mode=position_alloc,
    )

    with signal_backtest_overlay(patches):
        rot = run_portfolio_rotation(
            start,
            end,
            amounts,
            panels,
            mode="rotation",
            record_daily=True,
        )
        hold = run_portfolio_rotation(
            start,
            end,
            amounts,
            panels,
            mode="hold",
            use_pool=False,
            rotation_gate=False,
            record_daily=True,
        )

    full_edge = None
    if rot.return_pct is not None and hold.return_pct is not None:
        full_edge = rot.return_pct - hold.return_pct

    train_end = _day_before(valid_start)
    train_edge = _period_edge(rot, hold, start, train_end)
    valid_edge = _period_edge(rot, hold, valid_start, end)

    wfa_win = None
    wfa_mean = None
    if run_wfa_flag:
        with signal_backtest_overlay(patches):
            _, wfa_summary, _ = run_wfa(start, end, amounts, panels)
        n = wfa_summary.windows_with_buys or 1
        wfa_win = wfa_summary.rotation_wins_return / n
        wfa_mean = wfa_summary.mean_return_edge_pct

    score = composite_score(train_edge, valid_edge, full_edge, wfa_win, wfa_mean)
    elapsed = time.perf_counter() - t0
    return TrialResult(
        trial_id=trial_id,
        params=dict(params),
        score=score,
        full_edge_pct=full_edge,
        train_edge_pct=train_edge,
        valid_edge_pct=valid_edge,
        rotation_return_pct=rot.return_pct,
        hold_return_pct=hold.return_pct,
        wfa_win_rate=wfa_win,
        wfa_mean_edge_pct=wfa_mean,
        rotation_buys=rot.buy_count,
        rotation_sells=rot.sell_count,
        elapsed_sec=elapsed,
        label=label or _format_params_label(params),
        param_id=param_id,
        param_value=param_value,
    )


def _format_params_label(params: dict) -> str:
    parts = []
    for k, v in sorted(params.items()):
        if k == "position_alloc":
            continue
        spec = catalog_by_id().get(k)
        name = spec.label if spec else k
        parts.append(f"{name}={v:g}")
    return "; ".join(parts[:4]) + ("…" if len(parts) > 4 else "")


def run_screen(
    catalog: list[SignalParam],
    panels,
    start: str,
    end: str,
    valid_start: str,
    *,
    position_alloc: bool,
    baseline: TrialResult | None = None,
) -> tuple[list[ScreenRow], TrialResult]:
    if baseline is None:
        print("计算基准…")
        baseline = evaluate_trial(
            0,
            {},
            panels,
            start,
            end,
            valid_start,
            position_alloc=position_alloc,
            label="基准（默认阈值）",
        )
        print(
            f"  基准验证利差 {_fmt_pct(baseline.valid_edge_pct)} | "
            f"全段 {_fmt_pct(baseline.full_edge_pct)}"
        )

    rows: list[ScreenRow] = []
    trial_id = 1
    for i, spec in enumerate(catalog, 1):
        best_val = spec.default
        best_edge = baseline.valid_edge_pct
        sweep_rows = []
        for val in spec.sweep:
            params = {spec.id: val}
            tr = evaluate_trial(
                trial_id,
                params,
                panels,
                start,
                end,
                valid_start,
                position_alloc=position_alloc,
                label=f"{spec.label}={val:g}",
                param_id=spec.id,
                param_value=val,
            )
            trial_id += 1
            sweep_rows.append(
                {
                    "value": val,
                    "valid_edge_pct": tr.valid_edge_pct,
                    "full_edge_pct": tr.full_edge_pct,
                    "score": tr.score,
                }
            )
            if tr.valid_edge_pct is not None and (
                best_edge is None or tr.valid_edge_pct > best_edge
            ):
                best_edge = tr.valid_edge_pct
                best_val = val

        delta = None
        if best_edge is not None and baseline.valid_edge_pct is not None:
            delta = best_edge - baseline.valid_edge_pct

        threshold = spec.verdict_threshold_pct()
        verdict = "keep" if delta is not None and abs(delta) >= threshold else "drop"

        rows.append(
            ScreenRow(
                param_id=spec.id,
                label=spec.label,
                side=spec.side,
                index=spec.index,
                default=spec.default,
                best_value=best_val,
                best_valid_edge_pct=best_edge,
                baseline_valid_edge_pct=baseline.valid_edge_pct,
                edge_delta_pct=delta,
                verdict=verdict,
                sweep_results=sweep_rows,
            )
        )
        mark = "✓" if verdict == "keep" else "·"
        print(
            f"  [{i:>2}/{len(catalog)}] {mark} {spec.label:<28} "
            f"Δ验证 {_fmt_pct(delta):>8} → {verdict}"
        )

    return rows, baseline


def run_ablate(
    drop_params: list[SignalParam],
    panels,
    start: str,
    end: str,
    valid_start: str,
    *,
    position_alloc: bool,
    baseline: TrialResult | None = None,
) -> tuple[list[AblationRow], TrialResult]:
    if baseline is None:
        print("计算基准…")
        baseline = evaluate_trial(
            0,
            {},
            panels,
            start,
            end,
            valid_start,
            position_alloc=position_alloc,
            label="基准（全部条件开启）",
        )
        print(
            f"  基准验证利差 {_fmt_pct(baseline.valid_edge_pct)} | "
            f"买入 {baseline.rotation_buys} 卖出 {baseline.rotation_sells}"
        )

    rows: list[AblationRow] = []
    for i, spec in enumerate(drop_params, 1):
        off_val = ablate_value(spec)
        tr = evaluate_trial(
            i,
            {spec.id: off_val},
            panels,
            start,
            end,
            valid_start,
            position_alloc=position_alloc,
            label=f"关闭 {spec.label}",
            param_id=spec.id,
            param_value=off_val,
        )
        delta = None
        if tr.valid_edge_pct is not None and baseline.valid_edge_pct is not None:
            delta = tr.valid_edge_pct - baseline.valid_edge_pct
        buy_d = tr.rotation_buys - baseline.rotation_buys
        sell_d = tr.rotation_sells - baseline.rotation_sells
        changed = buy_d != 0 or sell_d != 0

        if not changed:
            verdict = "safe"
        elif delta is not None and abs(delta) >= 0.5:
            verdict = "material"
        else:
            verdict = "minor"

        rows.append(
            AblationRow(
                param_id=spec.id,
                label=spec.label,
                side=spec.side,
                index=spec.index,
                default=spec.default,
                ablated_value=off_val,
                valid_edge_pct=tr.valid_edge_pct,
                full_edge_pct=tr.full_edge_pct,
                edge_delta_pct=delta,
                rotation_buys=tr.rotation_buys,
                rotation_sells=tr.rotation_sells,
                buy_delta=buy_d,
                sell_delta=sell_d,
                signal_changed=changed,
                verdict=verdict,
            )
        )
        mark = {"safe": "○", "minor": "△", "material": "✗"}[verdict]
        chg = "信号变" if changed else "信号不变"
        print(
            f"  [{i:>2}/{len(drop_params)}] {mark} {spec.label:<28} "
            f"Δ验证 {_fmt_pct(delta):>8} 买{buy_d:+d} 卖{sell_d:+d} {chg}"
        )

    return rows, baseline


def _load_drop_params(side: str = "all") -> list[SignalParam]:
    screen_rows = _load_prior_screen()
    if not screen_rows:
        return []
    catalog = catalog_by_id()
    drops = [r for r in screen_rows if r.get("verdict") == "drop"]
    params = []
    for row in drops:
        spec = catalog.get(row["param_id"])
        if spec is None:
            continue
        if side != "all" and spec.side != side:
            continue
        params.append(spec)
    return params


def format_ablate_markdown(
    rows: list[AblationRow],
    baseline: TrialResult,
    meta: dict,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe = [r for r in rows if r.verdict == "safe"]
    minor = [r for r in rows if r.verdict == "minor"]
    material = [r for r in rows if r.verdict == "material"]

    lines = [
        "# 关闭条件试验（Ablation）",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} 至 {meta['end']}  ",
        f"> 验证段：{meta['valid_start']} 起  ",
        f"> 基准：验证利差 {_fmt_pct(baseline.valid_edge_pct)}，"
        f"买入 {baseline.rotation_buys} 次，卖出 {baseline.rotation_sells} 次  ",
        f"> 共 {len(rows)} 项 drop 指标 | 信号不变 {len(safe)} | "
        f"轻微变化 {len(minor)} | 实质影响 {len(material)}  ",
        "",
        "## 判定",
        "",
        "- **safe**：关闭后买卖次数与基准完全相同",
        "- **minor**：买卖次数有变，但验证段利差变化 < 0.5pct",
        "- **material**：验证段利差变化 ≥ 0.5pct（不宜删除该条件）",
        "",
        "## 可安全关闭（信号不变）",
        "",
        "| 指标 | 类型 | 指数 | 关闭值 | 验证利差Δ | 买Δ | 卖Δ |",
        "| --- | :---: | :---: | ---: | ---: | ---: | ---: |",
    ]
    for r in safe:
        lines.append(
            f"| {r.label} | {r.side} | {r.index or '—'} | {r.ablated_value:g} | "
            f"{_fmt_pct(r.edge_delta_pct)} | {r.buy_delta:+d} | {r.sell_delta:+d} |"
        )
    if not safe:
        lines.append("| （无） | | | | | | |")

    lines.extend([
        "",
        "## 有信号变化但组合影响小（minor）",
        "",
        "| 指标 | 类型 | 验证利差Δ | 买Δ | 卖Δ |",
        "| --- | :---: | ---: | ---: | ---: |",
    ])
    for r in sorted(minor, key=lambda x: abs(x.edge_delta_pct or 0), reverse=True):
        lines.append(
            f"| {r.label} | {r.side} | {_fmt_pct(r.edge_delta_pct)} | "
            f"{r.buy_delta:+d} | {r.sell_delta:+d} |"
        )
    if not minor:
        lines.append("| （无） | | | | |")

    lines.extend([
        "",
        "## 实质影响（material，勿删）",
        "",
        "| 指标 | 类型 | 验证利差Δ | 买Δ | 卖Δ |",
        "| --- | :---: | ---: | ---: | ---: |",
    ])
    for r in sorted(material, key=lambda x: abs(x.edge_delta_pct or 0), reverse=True):
        lines.append(
            f"| {r.label} | {r.side} | {_fmt_pct(r.edge_delta_pct)} | "
            f"{r.buy_delta:+d} | {r.sell_delta:+d} |"
        )
    if not material:
        lines.append("| （无） | | | | |")

    lines.append("")
    return "\n".join(lines)


def _normalize_search_params(raw: dict, space: dict[str, list[Any]]) -> dict:
    out = {}
    for k, v in raw.items():
        if k not in space:
            continue
        if isinstance(v, bool):
            out[k] = v
        else:
            out[k] = float(v)
    return out


def iter_grid(space: dict[str, list[Any]]):
    keys = list(space.keys())
    for combo in itertools.product(*(space[k] for k in keys)):
        yield _normalize_search_params(dict(zip(keys, combo)), space)


def sample_random(space: dict[str, list[Any]], rng: random.Random) -> dict:
    return _normalize_search_params({k: rng.choice(v) for k, v in space.items()}, space)


def search_grid(
    space: dict[str, list[Any]],
    evaluate: Callable[[int, dict], TrialResult],
) -> list[TrialResult]:
    results: list[TrialResult] = []
    for i, params in enumerate(iter_grid(space)):
        results.append(evaluate(i + 1, params))
        if (i + 1) % 20 == 0:
            print(f"  已完成 {i + 1} 组…")
    return results


def search_random(
    space: dict[str, list[Any]],
    trials: int,
    evaluate: Callable[[int, dict], TrialResult],
    seed: int,
) -> list[TrialResult]:
    rng = random.Random(seed)
    results: list[TrialResult] = []
    for i in range(trials):
        params = sample_random(space, rng)
        if not params:
            break
        results.append(evaluate(i + 1, params))
        if (i + 1) % 20 == 0:
            print(f"  已完成 {i + 1}/{trials} 次…")
    return results


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}%"


def print_top(results: list[TrialResult], top: int, valid_start: str):
    ranked = sorted(results, key=lambda x: x.score, reverse=True)[:top]
    print(f"\n=== Top {len(ranked)}（验证段自 {valid_start}）===")
    print(
        f"{'#':>3} {'得分':>7} {'验证利差':>9} {'训练利差':>9} {'全段利差':>9} "
        f"{'WFA胜率':>8} 参数"
    )
    print("-" * 96)
    for i, r in enumerate(ranked, 1):
        wfa = "—" if r.wfa_win_rate is None else f"{r.wfa_win_rate * 100:.0f}%"
        print(
            f"{i:>3} {r.score:>7.2f} {_fmt_pct(r.valid_edge_pct):>9} "
            f"{_fmt_pct(r.train_edge_pct):>9} {_fmt_pct(r.full_edge_pct):>9} "
            f"{wfa:>8} {r.label}"
        )
    return ranked


def format_screen_markdown(
    rows: list[ScreenRow],
    baseline: TrialResult,
    meta: dict,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    kept = [r for r in rows if r.verdict == "keep"]
    dropped = [r for r in rows if r.verdict == "drop"]
    lines = [
        "# 买卖点指标逐个验证",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} 至 {meta['end']}  ",
        f"> 验证段：{meta['valid_start']} 起  ",
        f"> 基准验证利差：{_fmt_pct(baseline.valid_edge_pct)} | "
        f"全段利差：{_fmt_pct(baseline.full_edge_pct)}  ",
        f"> 共 {len(rows)} 项 | 有影响 {len(kept)} | 可剔除 {len(dropped)}  ",
        "",
        "## 判定规则",
        "",
        "- 对每个指标在默认值附近试探 3–5 个水平",
        "- 若最佳水平相对基准的**验证段利差变化** < 阈值（买/卖 0.15pct，轮动 0.20pct），标记 **drop**",
        "- **drop** 参数不参与后续 `--task search`（除非 `--all-params`）",
        "",
        "## 有影响（keep）",
        "",
        "| 指标 | 类型 | 指数 | 默认 | 最优试探 | 验证利差Δ | 最优验证利差 |",
        "| --- | :---: | :---: | ---: | ---: | ---: | ---: |",
    ]
    for r in sorted(kept, key=lambda x: abs(x.edge_delta_pct or 0), reverse=True):
        lines.append(
            f"| {r.label} | {r.side} | {r.index or '—'} | {r.default:g} | "
            f"{r.best_value:g} | {_fmt_pct(r.edge_delta_pct)} | "
            f"{_fmt_pct(r.best_valid_edge_pct)} |"
        )
    lines.extend([
        "",
        "## 可剔除（drop）",
        "",
        "| 指标 | 类型 | 指数 | 默认 | 验证利差Δ |",
        "| --- | :---: | :---: | ---: | ---: |",
    ])
    for r in sorted(dropped, key=lambda x: x.label):
        lines.append(
            f"| {r.label} | {r.side} | {r.index or '—'} | {r.default:g} | "
            f"{_fmt_pct(r.edge_delta_pct)} |"
        )
    lines.extend([
        "",
        "## 下一步",
        "",
        "```bash",
        "python backtest_optimize.py --task search --method random --trials 200",
        "```",
        "",
    ])
    return "\n".join(lines)


def format_search_markdown(
    results: list[TrialResult],
    top_results: list[TrialResult],
    meta: dict,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 买卖点参数联合搜索",
        "",
        f"> 生成时间：{now}  ",
        f"> 区间：{meta['start']} 至 {meta['end']}  ",
        f"> 验证段：{meta['valid_start']}  ",
        f"> 方法：{meta['method']} | 试验：{meta['trials']} | 耗时：{meta['elapsed_sec']:.0f}s  ",
        "",
        "## Top 组合",
        "",
        "| 排名 | 得分 | 验证利差 | 训练利差 | 全段利差 | WFA胜率 | 参数摘要 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for i, r in enumerate(top_results, 1):
        wfa = "—" if r.wfa_win_rate is None else f"{r.wfa_win_rate * 100:.0f}%"
        lines.append(
            f"| {i} | {r.score:.2f} | {_fmt_pct(r.valid_edge_pct)} | "
            f"{_fmt_pct(r.train_edge_pct)} | {_fmt_pct(r.full_edge_pct)} | {wfa} | "
            f"{r.label} |"
        )
    best = top_results[0] if top_results else None
    if best:
        lines.extend([
            "",
            "## 最优参数",
            "",
            "```json",
            json.dumps(best.params, ensure_ascii=False, indent=2),
            "```",
        ])
    return "\n".join(lines)


def _load_prior_screen() -> list[dict] | None:
    path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("task") != "screen":
        return None
    return data.get("screen_rows")


def main(argv=None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="买卖点阈值筛选与搜索")
    parser.add_argument(
        "--task",
        choices=("screen", "search", "ablate"),
        default="screen",
        help="screen=逐个验证 search=联合搜索 ablate=关闭 drop 条件",
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=None)
    parser.add_argument("--valid-start", default=DEFAULT_VALID_START)
    parser.add_argument(
        "--side",
        choices=("all", "buy", "sell", "rotation"),
        default="all",
        help="screen 时仅验证指定类型",
    )
    parser.add_argument(
        "--method",
        choices=("grid", "random"),
        default="random",
        help="search 时搜索方法",
    )
    parser.add_argument("--trials", type=int, default=200)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--refine-wfa", action="store_true")
    parser.add_argument(
        "--all-params",
        action="store_true",
        help="search 时忽略 screen 结果，搜索全部参数",
    )
    parser.add_argument(
        "--position-alloc",
        choices=("true", "false"),
        default="true",
    )
    args = parser.parse_args(argv)

    end = _resolve_end(args.end)
    if pd.Timestamp(args.valid_start) <= pd.Timestamp(args.start):
        print("valid-start 必须晚于 start")
        return 1

    from backtest_buy_signals import get_panels
    from buy_amount_ranking import _preload_ranking_panels

    panels = get_panels()
    _preload_ranking_panels(panels)
    position_alloc = args.position_alloc == "true"

    catalog = build_signal_param_catalog()
    if args.side != "all":
        catalog = [p for p in catalog if p.side == args.side]

    t0 = time.perf_counter()

    if args.task == "screen":
        print(
            f"逐个验证 {len(catalog)} 项 | {args.start} → {end} | "
            f"验证段 {args.valid_start} 起"
        )
        rows, baseline = run_screen(
            catalog,
            panels,
            args.start,
            end,
            args.valid_start,
            position_alloc=position_alloc,
        )
        elapsed = time.perf_counter() - t0
        kept = sum(1 for r in rows if r.verdict == "keep")
        print(f"\n完成：有影响 {kept} / {len(rows)}，耗时 {elapsed:.0f}s")

        meta = {
            "task": "screen",
            "start": args.start,
            "end": end,
            "valid_start": args.valid_start,
            "side": args.side,
            "elapsed_sec": elapsed,
            "position_alloc": position_alloc,
        }
        BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        md_path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.md"
        json_path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.json"
        md_path.write_text(
            format_screen_markdown(rows, baseline, meta),
            encoding="utf-8",
        )
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "task": "screen",
                    "meta": meta,
                    "baseline": asdict(baseline),
                    "screen_rows": [asdict(r) for r in rows],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已保存: {md_path}")
        return 0

    if args.task == "ablate":
        drop_params = _load_drop_params(args.side)
        if not drop_params:
            print("无 drop 参数。请先运行: python backtest_optimize.py --task screen")
            return 1
        print(
            f"关闭条件试验 {len(drop_params)} 项 | {args.start} → {end} | "
            f"验证段 {args.valid_start} 起"
        )
        rows, baseline = run_ablate(
            drop_params,
            panels,
            args.start,
            end,
            args.valid_start,
            position_alloc=position_alloc,
        )
        elapsed = time.perf_counter() - t0
        safe = sum(1 for r in rows if r.verdict == "safe")
        minor = sum(1 for r in rows if r.verdict == "minor")
        material = sum(1 for r in rows if r.verdict == "material")
        print(
            f"\n完成：信号不变 {safe} | 轻微 {minor} | 实质影响 {material} | "
            f"耗时 {elapsed:.0f}s"
        )
        meta = {
            "task": "ablate",
            "start": args.start,
            "end": end,
            "valid_start": args.valid_start,
            "side": args.side,
            "elapsed_sec": elapsed,
            "position_alloc": position_alloc,
        }
        BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        md_path = BACKTEST_OUTPUT_DIR / f"{ABLATE_STEM}.md"
        json_path = BACKTEST_OUTPUT_DIR / f"{ABLATE_STEM}.json"
        md_path.write_text(
            format_ablate_markdown(rows, baseline, meta),
            encoding="utf-8",
        )
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "task": "ablate",
                    "meta": meta,
                    "baseline": asdict(baseline),
                    "rows": [asdict(r) for r in rows],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"报告已保存: {md_path}")
        return 0

    # --- search ---
    prior = None if args.all_params else _load_prior_screen()
    space = active_search_space(prior)
    if args.side != "all":
        ids = {p.id for p in catalog}
        space = {k: v for k, v in space.items() if k in ids}
    if not space:
        print("无可用搜索空间。请先运行 --task screen，或加 --all-params。")
        return 1

    n_keys = len(space)
    combo_n = 1
    for v in space.values():
        combo_n *= len(v)
    print(
        f"联合搜索 {n_keys} 个参数 | {args.method} | "
        f"空间大小约 {combo_n} | {args.start} → {end}"
    )

    def evaluate_fn(trial_id: int, params: dict) -> TrialResult:
        return evaluate_trial(
            trial_id,
            params,
            panels,
            args.start,
            end,
            args.valid_start,
            position_alloc=position_alloc,
        )

    if args.method == "grid":
        if combo_n > 500:
            print(f"网格过大（{combo_n}），请改用 --method random")
            return 1
        results = search_grid(space, evaluate_fn)
    else:
        results = search_random(space, args.trials, evaluate_fn, args.seed)

    elapsed = time.perf_counter() - t0
    print(f"搜索完成，{len(results)} 次试验，耗时 {elapsed:.0f}s")

    if args.refine_wfa and results:
        top_n = min(args.top, len(results))
        ranked_pre = sorted(results, key=lambda x: x.score, reverse=True)[:top_n]
        print(f"\n对 Top {top_n} 重跑 WFA…")
        refined = []
        for i, old in enumerate(ranked_pre):
            tr = evaluate_trial(
                old.trial_id,
                old.params,
                panels,
                args.start,
                end,
                args.valid_start,
                position_alloc=position_alloc,
                run_wfa_flag=True,
            )
            refined.append(tr)
            print(
                f"  [{i + 1}/{top_n}] 得分 {tr.score:.2f} "
                f"WFA {(tr.wfa_win_rate or 0) * 100:.0f}%"
            )
        refined_ids = {r.trial_id for r in refined}
        results = refined + [r for r in results if r.trial_id not in refined_ids]

    top_results = print_top(results, args.top, args.valid_start)

    meta = {
        "task": "search",
        "start": args.start,
        "end": end,
        "valid_start": args.valid_start,
        "method": args.method,
        "trials": len(results),
        "param_count": n_keys,
        "seed": args.seed,
        "elapsed_sec": elapsed,
        "all_params": args.all_params,
        "position_alloc": position_alloc,
    }
    BACKTEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.md"
    json_path = BACKTEST_OUTPUT_DIR / f"{OUTPUT_STEM}.json"
    md_path.write_text(format_search_markdown(results, top_results, meta), encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "task": "search",
                "meta": meta,
                "top": [asdict(r) for r in top_results],
                "all_trials": [asdict(r) for r in sorted(results, key=lambda x: -x.score)],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n报告已保存: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
