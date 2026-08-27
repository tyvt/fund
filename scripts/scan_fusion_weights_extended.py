#!/usr/bin/env python
"""Exhaustive 0.05-grid scan for the selected fusion factors."""

from __future__ import annotations

import argparse
import itertools
import json
import math
from pathlib import Path
from typing import Any

import duckdb
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "fusion_scan"
FACTOR_ROOT = ROOT / "data" / "parquet" / "factors"
STOCK_GLOB = ROOT / "data" / "parquet" / "stock_daily" / "year=*" / "*.parquet"
QFQ_GLOB = ROOT / "data" / "parquet" / "stock_daily_qfq" / "year=*" / "*.parquet"


def _path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def load_selected(path: Path) -> tuple[list[str], dict[str, int]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    factors = [str(value) for value in payload["factors"]]
    directions = {str(k): int(v) for k, v in payload.get("directions", {}).items()}
    if not 5 <= len(factors) <= 6:
        raise ValueError(f"selected factor count must be 5-6, got {len(factors)}")
    return factors, directions


def load_panel(factors: list[str], start: str, end: str) -> pd.DataFrame:
    aliases = {factor: f"f{i}" for i, factor in enumerate(factors)}
    scans = []
    for factor, alias in aliases.items():
        glob = _path(FACTOR_ROOT / factor / "year=*" / "*.parquet")
        scans.append(
            f"{alias} AS (SELECT try_cast(trade_date AS DATE) trade_date, "
            f"symbol, value FROM read_parquet('{glob}', hive_partitioning=true) "
            f"WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' AND DATE '{end}')"
        )
    base = aliases[factors[0]]
    joins = []
    columns = []
    for factor in factors:
        alias = aliases[factor]
        columns.append(f"{alias}.value AS {factor}")
        if alias != base:
            joins.append(
                f"JOIN {alias} ON {alias}.trade_date={base}.trade_date "
                f"AND {alias}.symbol={base}.symbol"
            )
    stock = _path(STOCK_GLOB)
    qfq = _path(QFQ_GLOB)
    sql = f"""
    WITH
    {', '.join(scans)},
    first_seen AS (
      SELECT symbol, min(try_cast(trade_date AS DATE)) AS listed_date
      FROM read_parquet('{stock}', hive_partitioning=true) GROUP BY symbol
    ),
    daily AS (
      SELECT try_cast(trade_date AS DATE) trade_date, symbol,
             max(amount) amount, max(float_mv) float_mv, max(is_st) is_st
      FROM read_parquet('{stock}', hive_partitioning=true)
      WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' AND DATE '{end}'
      GROUP BY trade_date, symbol
    ),
    qfq_raw AS (
      SELECT try_cast(trade_date AS DATE) trade_date, symbol, max(close) AS close_price
      FROM read_parquet('{qfq}', hive_partitioning=true)
      WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{start}' - INTERVAL 5 DAY
        AND DATE '{end}' + INTERVAL 45 DAY
      GROUP BY trade_date, symbol
    ),
    qfq_forward AS (
      SELECT trade_date, symbol,
             lead(close_price, 20) OVER (PARTITION BY symbol ORDER BY trade_date)
               / nullif(close_price, 0) - 1.0 AS forward_return
      FROM qfq_raw
    )
    SELECT {base}.trade_date, {base}.symbol, {', '.join(columns)}, q.forward_return
    FROM {base}
    {' '.join(joins)}
    JOIN daily d ON d.trade_date={base}.trade_date AND d.symbol={base}.symbol
    JOIN first_seen fs ON fs.symbol={base}.symbol
    JOIN qfq_forward q ON q.trade_date={base}.trade_date AND q.symbol={base}.symbol
    WHERE coalesce(d.is_st, 1)=0 AND d.amount >= 1000000 AND d.float_mv >= 500000000
      AND date_diff('day', fs.listed_date, {base}.trade_date) >= 365
    """
    with duckdb.connect() as con:
        frame = con.execute(sql).fetchdf()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame.replace([np.inf, -np.inf], np.nan).dropna(
        subset=[*factors, "forward_return"]
    )


def weight_grid(count: int, minimum: float, maximum: float, step: float) -> np.ndarray:
    units = int(round(1.0 / step))
    low = int(round(minimum / step))
    high = int(round(maximum / step))
    rows = [
        values
        for values in itertools.product(range(low, high + 1), repeat=count)
        if sum(values) == units
    ]
    return np.asarray(rows, dtype=np.float32) * np.float32(step)


def prepare_months(
    panel: pd.DataFrame, factors: list[str], directions: dict[str, int]
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    symbol_ids = {symbol: i for i, symbol in enumerate(sorted(panel["symbol"].unique()))}
    months: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    for _, group in panel.groupby("trade_date", sort=True, observed=True):
        ranked = pd.DataFrame(index=group.index)
        for factor in factors:
            values = pd.to_numeric(group[factor], errors="coerce") * (
                1 if directions.get(factor, 1) >= 0 else -1
            )
            ranked[factor] = values.rank(pct=True, method="average")
        valid = ranked.notna().all(axis=1) & group["forward_return"].notna()
        if valid.sum() < 100:
            continue
        months.append(
            (
                ranked.loc[valid, factors].to_numpy(np.float32),
                group.loc[valid, "forward_return"].to_numpy(np.float32),
                group.loc[valid, "symbol"].map(symbol_ids).to_numpy(np.int32),
            )
        )
    return months


def evaluate_grid(
    months: list[tuple[np.ndarray, np.ndarray, np.ndarray]],
    weights: np.ndarray,
    *,
    top_n: int = 20,
    batch_size: int = 512,
    round_trip_cost: float = 0.0023,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    count = len(weights)
    sharpes = np.full(count, np.nan, dtype=np.float64)
    returns = np.full(count, np.nan, dtype=np.float64)
    turnovers = np.full(count, np.nan, dtype=np.float64)
    for offset in range(0, count, batch_size):
        batch = weights[offset : offset + batch_size]
        size = len(batch)
        monthly = np.empty((len(months), size), dtype=np.float32)
        monthly_turnover = np.zeros((len(months), size), dtype=np.float32)
        previous: np.ndarray | None = None
        for month_index, (x, forward_return, symbol_ids) in enumerate(months):
            score = x @ batch.T
            positions = np.argpartition(score, -top_n, axis=0)[-top_n:, :]
            monthly[month_index] = np.take_along_axis(
                np.broadcast_to(forward_return[:, None], score.shape), positions, axis=0
            ).mean(axis=0)
            current = symbol_ids[positions]
            if previous is not None:
                overlap = (current[:, None, :] == previous[None, :, :]).any(axis=1).sum(axis=0)
                monthly_turnover[month_index] = 1.0 - overlap / float(top_n)
            previous = current
        net = monthly - monthly_turnover * np.float32(round_trip_cost)
        mean = net.mean(axis=0, dtype=np.float64)
        std = net.std(axis=0, ddof=1, dtype=np.float64)
        target = slice(offset, offset + size)
        sharpes[target] = np.divide(
            mean * math.sqrt(12.0), std, out=np.full(size, np.nan), where=std > 0
        )
        returns[target] = np.power(np.prod(1.0 + net, axis=0, dtype=np.float64), 12.0 / len(net)) - 1.0
        turnovers[target] = monthly_turnover[1:].mean(axis=0, dtype=np.float64)
    return sharpes, returns, turnovers


def plot_heatmap(results: pd.DataFrame, factors: list[str], path: Path) -> None:
    first, second = factors[:2]
    pivot = results.pivot_table(
        index=first, columns=second, values="sharpe", aggfunc="max"
    ).sort_index(ascending=False)
    fig, ax = plt.subplots(figsize=(9, 7))
    image = ax.imshow(pivot, aspect="auto", cmap="RdYlGn")
    ax.set_xticks(range(len(pivot.columns)), [f"{v:.2f}" for v in pivot.columns])
    ax.set_yticks(range(len(pivot.index)), [f"{v:.2f}" for v in pivot.index])
    ax.set_xlabel(second)
    ax.set_ylabel(first)
    ax.set_title("Maximum Sharpe across remaining factor weights")
    fig.colorbar(image, ax=ax, label="Sharpe")
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Exhaustive fusion weight scan")
    parser.add_argument("--factors-file", default="output/orthogonality/selected_factors.json")
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--min", type=float, default=0.05)
    parser.add_argument("--max", type=float, default=0.60)
    parser.add_argument("--step", type=float, default=0.05)
    parser.add_argument("--batch-size", type=int, default=512)
    args = parser.parse_args()
    factors, directions = load_selected(ROOT / args.factors_file)
    print(f"Loading {len(factors)} factors...", flush=True)
    panel = load_panel(factors, args.start, args.end)
    months = prepare_months(panel, factors, directions)
    grid = weight_grid(len(factors), args.min, args.max, args.step)
    print(f"Evaluating {len(grid):,} combinations over {len(months)} months...", flush=True)
    sharpe, annual_return, turnover = evaluate_grid(
        months, grid, batch_size=args.batch_size
    )
    results = pd.DataFrame(grid, columns=factors)
    results["sharpe"] = sharpe
    results["annual_return"] = annual_return
    results["monthly_one_way_turnover"] = turnover
    results = results.sort_values(
        ["sharpe", "annual_return"], ascending=False, ignore_index=True
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    results.head(10).to_csv(OUTPUT / "top_10_weights.csv", index=False)
    results.to_parquet(OUTPUT / "all_weight_results.parquet", index=False)
    plot_heatmap(results, factors, OUTPUT / "weight_heatmap.png")
    best: dict[str, Any] = {
        "factors": factors,
        "directions": directions,
        "weights": {factor: float(results.loc[0, factor]) for factor in factors},
        "sharpe": float(results.loc[0, "sharpe"]),
        "annual_return": float(results.loc[0, "annual_return"]),
        "monthly_one_way_turnover": float(results.loc[0, "monthly_one_way_turnover"]),
        "combinations": int(len(results)),
        "months": int(len(months)),
        "method": "20-trading-day equal-weight top20, hard universe, explicit 23bp turnover cost",
    }
    (OUTPUT / "best_weights.json").write_text(
        json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(best, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
