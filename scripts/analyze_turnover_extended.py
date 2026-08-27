#!/usr/bin/env python
"""Measure the independent turnover effects of hold bonus and cost hurdle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.scan_fusion_weights_extended import load_panel, load_selected
from vbt.strategies.dividend_lowvol import build_cost_aware_selection
from vbt.strategies.signal_generators import compute_fusion_score


def main() -> int:
    parser = argparse.ArgumentParser(description="Fusion turnover-control attribution")
    parser.add_argument("--config", default="config/vectorbt/ablation_config.yaml")
    parser.add_argument("--tag", default="fusion_v2")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2024-12-31")
    args = parser.parse_args()
    factors_file = ROOT / "output" / "orthogonality" / "selected_factors.json"
    best_file = ROOT / "output" / "fusion_scan" / "best_weights.json"
    factors, directions = load_selected(factors_file)
    weights = json.loads(best_file.read_text(encoding="utf-8"))["weights"]
    print("Loading point-in-time monthly factor panel...", flush=True)
    panel = load_panel(factors, args.start, args.end)
    matrices = {
        factor: panel.pivot(index="trade_date", columns="symbol", values=factor)
        for factor in factors
    }
    score = compute_fusion_score(
        matrices, weights=weights, factors=factors, directions=directions
    )
    hard = score.notna()
    variants = {
        "no_control": (0.0, 0.0),
        "hold_bonus_only": (0.10, 0.0),
        "cost_threshold_only": (0.0, 0.01),
        "hold_bonus_and_cost": (0.10, 0.01),
    }
    rows = []
    for name, (bonus, threshold) in variants.items():
        selected, trades = build_cost_aware_selection(
            score,
            hard_eligible=hard,
            candidate_n=100,
            top_n=20,
            hold_bonus=bonus,
            cost_threshold=threshold,
        )
        turnover = selected.astype(float).diff().abs().sum(axis=1).mul(0.5 / 20.0)
        turnover.iloc[0] = 0.0
        rows.append(
            {
                "variant": name,
                "hold_bonus": bonus,
                "cost_threshold": threshold,
                "mean_monthly_one_way_turnover": float(turnover.mean()),
                "median_monthly_one_way_turnover": float(turnover.median()),
                "p95_monthly_one_way_turnover": float(turnover.quantile(0.95)),
                "average_holdings": float(
                    selected.sum(axis=1).mean()
                ),
                "total_buys": int(sum(len(item["buy"]) for item in trades.values())),
                "total_sells": int(sum(len(item["sell"]) for item in trades.values())),
            }
        )
        print(f"{name}: turnover={rows[-1]['mean_monthly_one_way_turnover']:.2%}")
    result = pd.DataFrame(rows)
    baseline = float(result.loc[result["variant"].eq("no_control"), "mean_monthly_one_way_turnover"].iloc[0])
    result["turnover_reduction_vs_no_control"] = 1.0 - (
        result["mean_monthly_one_way_turnover"] / baseline
    )
    output = ROOT / "output" / "turnover"
    output.mkdir(parents=True, exist_ok=True)
    result.to_csv(output / "turnover_control_comparison.csv", index=False)
    payload = {
        "start": args.start,
        "end": args.end,
        "results": result.to_dict(orient="records"),
        "buffer_dependency": 0.0,
        "note": "fusion_v2 does not use the legacy max-sell buffer",
    }
    (output / "turnover_analysis.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
