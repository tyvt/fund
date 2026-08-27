#!/usr/bin/env python
"""Apply incremental-capital deployment to real fusion_v2 target holdings."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vbt.strategies.capital_deployment import deploy_new_capital


def main() -> int:
    parser = argparse.ArgumentParser(description="Incremental-capital deployment")
    parser.add_argument("--tag", default="fusion_v2")
    parser.add_argument("--new-capital", type=float, default=100000.0)
    parser.add_argument("--current-value", type=float, default=100000.0)
    parser.add_argument("--max-daily-trade", type=float, default=0.20)
    parser.add_argument("--market-overvalued", action="store_true")
    args = parser.parse_args()
    source = ROOT / "output" / "ablation" / f"metrics_{args.tag}.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    selections = payload["full_execution"]["selected_symbols"]
    dates = sorted(selections)
    if len(dates) < 2:
        raise ValueError("deployment backtest requires at least two rebalance selections")
    previous, target = selections[dates[-2]], selections[dates[-1]]
    current = {
        symbol: args.current_value / max(len(previous), 1) for symbol in previous
    }
    targets = [
        {
            "symbol": symbol,
            "score": 1.0 - rank / max(len(target), 1),
            "target_weight": 1.0 / max(len(target), 1),
        }
        for rank, symbol in enumerate(target)
    ]
    remaining = float(args.new_capital)
    days = []
    for day in range(1, 32):
        plan = deploy_new_capital(
            current,
            targets,
            remaining,
            args.max_daily_trade,
            market_overvalued=args.market_overvalued,
        )
        for order in plan["orders"]:
            current[order["symbol"]] = current.get(order["symbol"], 0.0) + order["amount"]
        days.append({"deployment_day": day, **plan})
        remaining = float(plan["cash_remaining"])
        if remaining <= 1e-8 or plan["invested"] <= 1e-8:
            break
    result = {
        "source": str(source),
        "from_rebalance": dates[-2],
        "to_rebalance": dates[-1],
        "new_capital": args.new_capital,
        "deployed": args.new_capital - remaining,
        "cash_remaining": remaining,
        "days": days,
    }
    output = ROOT / "output" / "deployment"
    output.mkdir(parents=True, exist_ok=True)
    (output / f"deployment_{args.tag}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in result.items() if k != "days"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
