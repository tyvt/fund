#!/usr/bin/env python
"""Walk-forward 滚动窗口验证：对每段滚动区间重跑三指数回测，检验参数稳健性。

不修改策略代码；通过 SELL_SIMPLIFY_MODE 环境变量选择卖出模式（off/a/b）。
每个窗口独立调用 backtest_all(start, end)，输出各指数 XIRR / 最大回撤 / 买卖次数，
用于判断收益是否集中在个别窗口（稳健性诊断），不可外推为未来收益。

用法：
  python scripts/walk_forward_validation.py --window 3 --step 1
  SELL_SIMPLIFY_MODE=a python scripts/walk_forward_validation.py --window 3 --step 1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_trade_signals import backtest_all  # noqa: E402

DEFAULT_START = "2015-01-01"
DEFAULT_END = "2026-12-31"
OUTPUT = ROOT / "output" / "walk_forward"


def _year_bounds(start_year: int, end_year: int) -> tuple[str, str]:
    return f"{start_year}-01-01", f"{end_year}-12-31"


def build_windows(
    start_year: int, end_year: int, window: int, step: int
) -> list[tuple[int, int, str, str]]:
    """滚动窗口切分：(窗口序号, 结束年, start, end)。

    - 首个窗口 [start_year, start_year+window-1]；
    - 之后每步推进 step 年，窗口长度 window 不变；
    - 末窗若超过 end_year 则截断到 end_year（不足整窗仍纳入，至少两年）；
    - 边界：window>=1, step>=1, end_year>=start_year+1，否则返回空。
    """
    if window < 1 or step < 1 or end_year < start_year + 1:
        return []
    windows = []
    cursor = start_year
    idx = 0
    while cursor <= end_year:
        win_end = min(cursor + window - 1, end_year)
        if win_end - cursor + 1 < 2:  # 至少两年才有滚动意义
            break
        s, e = _year_bounds(cursor, win_end)
        windows.append((idx, win_end, s, e))
        if win_end >= end_year:
            break
        cursor += step
        idx += 1
    return windows


def _slim(result) -> dict:
    """从 TradeResult 提取诊断所需字段。"""
    d = asdict(result) if not isinstance(result, dict) else dict(result)
    keys = ("code", "name", "xirr_pct", "max_drawdown_pct", "return_pct",
            "annualized_return_pct", "buy_count", "sell_count", "sharpe_ratio")
    return {k: d.get(k) for k in keys}


def run_window(idx: int, end_year: int, start: str, end: str, mode: str) -> dict:
    results, _ = backtest_all(start_date=start, end_date=end)
    per_index = [_slim(r) for r in (results or [])]
    return {
        "window_index": idx,
        "end_year": end_year,
        "start": start,
        "end": end,
        "sell_mode": mode,
        "indices": per_index,
    }


def _aggregate(windows: list[dict]) -> dict:
    """按指数汇总各窗 XIRR：均值/最小值/为正窗口数，用于稳健性判读。"""
    by_code: dict[str, list[float]] = {}
    for w in windows:
        for item in w["indices"]:
            xirr = item.get("xirr_pct")
            if xirr is None:
                continue
            by_code.setdefault(item["code"], []).append(float(xirr))
    summary = {}
    for code, vals in by_code.items():
        summary[code] = {
            "windows": len(vals),
            "xirr_mean_pct": round(sum(vals) / len(vals), 2),
            "xirr_min_pct": round(min(vals), 2),
            "xirr_max_pct": round(max(vals), 2),
            "positive_windows": sum(1 for v in vals if v > 0),
        }
    return summary


def _markdown(windows: list[dict], summary: dict, mode: str) -> str:
    lines = [
        "# Walk-forward 滚动窗口诊断",
        "",
        f"卖出模式：`{mode}`　窗口数：{len(windows)}",
        "",
        "> 仅作参数稳健性诊断：观察收益是否集中在个别窗口；不可外推未来收益。",
        "",
        "## 各窗口明细",
        "",
        "| 窗 | 区间 | 指数 | XIRR% | 回撤% | 收益% | 买 | 卖 |",
        "|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for w in windows:
        span = f"{w['start'][:7]}~{w['end'][:7]}"
        for it in w["indices"]:
            def fmt(key):
                v = it.get(key)
                return "" if v is None else f"{float(v):.1f}"
            lines.append(
                f"| {w['window_index']} | {span} | {it.get('name') or it.get('code')} | "
                f"{fmt('xirr_pct')} | {fmt('max_drawdown_pct')} | {fmt('return_pct')} | "
                f"{it.get('buy_count', '')} | {it.get('sell_count', '')} |"
            )
    lines += ["", "## 按指数汇总", "", "| 指数 | 窗口 | XIRR均值% | 最差% | 最好% | 正收益窗 |",
              "|---|---:|---:|---:|---:|---:|"]
    for code, s in summary.items():
        lines.append(
            f"| {code} | {s['windows']} | {s['xirr_mean_pct']} | {s['xirr_min_pct']} | "
            f"{s['xirr_max_pct']} | {s['positive_windows']} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Walk-forward 滚动窗口验证")
    p.add_argument("--start-year", type=int, default=2015)
    p.add_argument("--end-year", type=int, default=2026)
    p.add_argument("--window", type=int, default=3, help="窗口长度（年）")
    p.add_argument("--step", type=int, default=1, help="滚动步长（年）")
    p.add_argument("--mode", choices=["off", "a", "b"], default=None,
                   help="卖出模式；覆盖环境变量 SELL_SIMPLIFY_MODE")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    mode = args.mode or os.environ.get("SELL_SIMPLIFY_MODE", "off").strip().lower() or "off"
    if mode in {"off", "a", "b"}:
        os.environ["SELL_SIMPLIFY_MODE"] = mode
    windows_spec = build_windows(args.start_year, args.end_year, args.window, args.step)
    if not windows_spec:
        print("窗口参数无效或无有效窗口（需 end_year>=start_year+1, window>=1, step>=1）")
        return 2
    windows = []
    for idx, end_year, start, end in windows_spec:
        print(f"[walk-forward] 窗口 {idx}: {start[:10]} ~ {end[:10]}", flush=True)
        windows.append(run_window(idx, end_year, start, end, mode))
    summary = _aggregate(windows)
    payload = {"sell_mode": mode, "window_years": args.window, "step_years": args.step,
               "windows": windows, "summary": summary}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    tag = mode
    (OUTPUT / f"walk_forward_{tag}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (OUTPUT / f"walk_forward_{tag}.md").write_text(
        _markdown(windows, summary, mode), encoding="utf-8"
    )
    print(f"[walk-forward] 完成 {len(windows)} 个窗口，结果写入 {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
