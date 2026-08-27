#!/usr/bin/env python
"""Validate deterministic incremental-capital deployment at three scales."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vbt.strategies.capital_deployment import deploy_new_capital


OUTPUT = ROOT / "output" / "validation" / "incremental_capital_results.json"


def parse_capital_levels(value: str) -> list[float]:
    try:
        levels = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("capital-levels 必须为逗号分隔数字") from exc
    if not levels or any(level <= 0.0 for level in levels):
        raise argparse.ArgumentTypeError("capital-levels 必须全部大于 0")
    return levels


def simulate_injection(
    previous: Sequence[str],
    target: Sequence[str],
    *,
    current_value: float,
    new_capital: float,
    max_daily_trade: float = 0.20,
    max_days: int = 30,
) -> dict[str, object]:
    if not target:
        raise ValueError("目标持仓不得为空")
    current = {
        str(symbol): float(current_value) / max(len(previous), 1)
        for symbol in previous
    }
    targets = [
        {
            "symbol": str(symbol),
            "score": 1.0 - rank / max(len(target), 1),
            "target_weight": 1.0 / len(target),
        }
        for rank, symbol in enumerate(target)
    ]
    remaining = float(new_capital)
    days = []
    daily_cap_respected = True
    target_set = set(map(str, target))
    target_only = True
    for day in range(1, max_days + 1):
        plan = deploy_new_capital(
            current,
            targets,
            remaining,
            max_daily_trade,
        )
        daily_cap_respected &= float(plan["invested"]) <= float(plan["daily_limit"]) + 1e-8
        target_only &= all(order["symbol"] in target_set for order in plan["orders"])
        for order in plan["orders"]:
            symbol = str(order["symbol"])
            current[symbol] = current.get(symbol, 0.0) + float(order["amount"])
        days.append({"deployment_day": day, **plan})
        remaining = float(plan["cash_remaining"])
        if remaining <= max(1e-8, new_capital * 1e-10):
            break
        if float(plan["invested"]) <= 1e-8:
            break
    deployed = float(new_capital - remaining)
    deployment_ratio = deployed / float(new_capital)
    passed = deployment_ratio >= 0.99 and daily_cap_respected and target_only
    return {
        "new_capital": float(new_capital),
        "current_portfolio_value": float(current_value),
        "target_count": len(target),
        "deployed": deployed,
        "cash_remaining": remaining,
        "deployment_ratio": deployment_ratio,
        "deployment_days": len(days),
        "daily_cap_respected": bool(daily_cap_respected),
        "target_only": bool(target_only),
        "passed": bool(passed),
        "days": days,
    }


def run_incremental_capital_test(
    tag: str,
    *,
    scenarios: int = 3,
    capital_levels: Sequence[float] = (100000.0, 200000.0, 500000.0),
    output: Path = OUTPUT,
) -> dict[str, object]:
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag)).strip("_")
    if scenarios <= 0:
        raise ValueError("scenarios 必须大于 0")
    if len(capital_levels) < scenarios:
        raise ValueError("capital-levels 数量不得少于 scenarios")
    metrics_path = ROOT / "output" / "ablation" / f"metrics_{safe_tag}.json"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    selections = payload["full_execution"]["selected_symbols"]
    dates = sorted(selections)
    if len(dates) < 2:
        raise ValueError("增量资金验收至少需要两个调仓截面")
    previous = selections[dates[-2]]
    target = selections[dates[-1]]
    current_value = float(payload.get("config", {}).get("backtest", {}).get("initial_capital", 100000.0))
    results = [
        simulate_injection(
            previous,
            target,
            current_value=current_value,
            new_capital=float(capital),
        )
        for capital in list(capital_levels)[:scenarios]
    ]
    result: dict[str, object] = {
        "tag": safe_tag,
        "source_metrics": str(metrics_path.relative_to(ROOT)),
        "from_rebalance": dates[-2],
        "to_rebalance": dates[-1],
        "scenario_count": int(scenarios),
        "pass_rule": "deployment_ratio >= 99%, daily cap respected, target-only orders",
        "scenarios": results,
        "passed": bool(all(item["passed"] for item in results) and len(results) == scenarios),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fusion_v2 增量资金三场景验收")
    parser.add_argument("--tag", default="fusion_v2_fixed")
    parser.add_argument("--scenarios", type=int, default=3)
    parser.add_argument(
        "--capital-levels",
        type=parse_capital_levels,
        default=parse_capital_levels("100000,200000,500000"),
    )
    parser.add_argument("--output", type=Path, default=OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    result = run_incremental_capital_test(
        args.tag,
        scenarios=args.scenarios,
        capital_levels=args.capital_levels,
        output=output,
    )
    concise = {key: value for key, value in result.items() if key != "scenarios"}
    concise["scenarios"] = [
        {key: value for key, value in item.items() if key != "days"}
        for item in result["scenarios"]
    ]
    print(json.dumps(concise, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
