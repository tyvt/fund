#!/usr/bin/env python
"""Build a capital-to-holding-count suitability matrix using actual A-share lots."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Sequence

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.incremental_capital_test import parse_capital_levels


RAW_GLOB = ROOT / "data" / "parquet" / "stock_daily" / "year=*" / "*.parquet"
OUTPUT = ROOT / "output" / "holding_matrix"


def load_latest_prices(symbols: Sequence[str], as_of: str) -> pd.Series:
    escaped = ",".join(f"'{str(symbol).replace(chr(39), chr(39)*2)}'" for symbol in symbols)
    path = RAW_GLOB.resolve().as_posix().replace("'", "''")
    with duckdb.connect() as con:
        frame = con.execute(
            f"""
            SELECT symbol, close_price
            FROM (
              SELECT symbol, try_cast(close AS DOUBLE) close_price,
                     row_number() OVER (
                       PARTITION BY symbol ORDER BY try_cast(trade_date AS DATE) DESC
                     ) row_number
              FROM read_parquet('{path}', hive_partitioning=true, union_by_name=true)
              WHERE symbol IN ({escaped})
                AND try_cast(trade_date AS DATE) <= DATE '{as_of}'
                AND try_cast(close AS DOUBLE) > 0
            ) ranked
            WHERE row_number = 1
            """
        ).fetchdf()
    return frame.set_index("symbol")["close_price"].astype(float)


def build_holding_matrix(
    prices: pd.Series,
    capital_levels: Sequence[float],
    *,
    target_holdings: int,
    lot_size: int = 100,
    buy_cost_rate: float = 0.0013,
) -> pd.DataFrame:
    if target_holdings <= 0 or lot_size <= 0:
        raise ValueError("target_holdings 和 lot_size 必须大于 0")
    lot_costs = pd.to_numeric(prices, errors="coerce").dropna() * lot_size * (1.0 + buy_cost_rate)
    rows = []
    for capital in capital_levels:
        amount = float(capital)
        slot_budget = amount / target_holdings
        affordable = int(lot_costs.le(slot_budget).sum())
        coverage = min(1.0, affordable / target_holdings)
        recommended = target_holdings if coverage >= 0.80 else max(1, affordable)
        rows.append(
            {
                "capital": amount,
                "target_holdings": int(target_holdings),
                "recommended_holdings": int(recommended),
                "average_amount": amount / recommended,
                "affordable_target_stocks": affordable,
                "coverage": coverage,
                "status": "适配" if coverage >= 0.80 else "需降低持仓数或增加资金",
            }
        )
    return pd.DataFrame(rows)


def run_holding_matrix(tag: str, capital_levels: Sequence[float]) -> dict[str, object]:
    safe_tag = re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag)).strip("_")
    metrics_path = ROOT / "output" / "ablation" / f"metrics_{safe_tag}.json"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    selections = payload["full_execution"]["selected_symbols"]
    latest_date = sorted(selections)[-1]
    symbols = selections[latest_date]
    prices = load_latest_prices(symbols, latest_date)
    frame = build_holding_matrix(
        prices,
        capital_levels,
        target_holdings=len(symbols),
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    frame.to_csv(OUTPUT / "holding_matrix.csv", index=False, encoding="utf-8-sig")
    result: dict[str, object] = {
        "tag": safe_tag,
        "as_of": latest_date,
        "target_symbols": list(symbols),
        "price_coverage": int(len(prices)),
        "lot_size": 100,
        "buy_cost_rate": 0.0013,
        "coverage_definition": "实际一手成本不高于等额持仓槽预算的目标股票数 / 目标持仓数",
        "rows": frame.to_dict(orient="records"),
    }
    (OUTPUT / "holding_matrix.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fusion_v2 资金持仓适配矩阵")
    parser.add_argument("--tag", default="fusion_v2_fixed")
    parser.add_argument(
        "--capital-levels",
        "--capital",
        type=parse_capital_levels,
        default=parse_capital_levels("100000,200000,500000,1000000"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_holding_matrix(args.tag, args.capital_levels)
    print(pd.DataFrame(result["rows"]).to_string(index=False))
    print(f"持仓适配矩阵：{OUTPUT / 'holding_matrix.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
