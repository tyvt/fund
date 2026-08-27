#!/usr/bin/env python
"""Execution-timing, missing-fill and block-bootstrap robustness tests."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vbt.adapters import DEFAULT_FACTORS, VBTDataLoader
from vbt.strategies import AblationStrategy
from scripts.run_ablation import experiment_params, load_config, strategy_params


def _safe_tag(tag: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", str(tag)).strip("_")


def _summary(values: np.ndarray) -> dict[str, float]:
    clean = np.asarray(values, dtype=float)
    clean = clean[np.isfinite(clean)]
    return {
        "mean": float(np.mean(clean)),
        "std": float(np.std(clean, ddof=1)),
        "p05": float(np.quantile(clean, 0.05)),
        "p50": float(np.quantile(clean, 0.50)),
        "p95": float(np.quantile(clean, 0.95)),
    }


def _annual_return(nav: pd.Series) -> float:
    if len(nav) <= 1 or float(nav.iloc[0]) <= 0.0:
        return float("nan")
    return float((nav.iloc[-1] / nav.iloc[0]) ** (252.0 / (len(nav) - 1)) - 1.0)


def simulate_sparse_targets(
    close: pd.DataFrame,
    targets: pd.DataFrame,
    event_dates: list[pd.Timestamp],
    *,
    commission: float,
    stamp_duty: float,
    slippage: float,
) -> pd.Series:
    """Replay sparse target weights with daily weight drift and explicit costs."""
    prices = close.astype(float).ffill()
    dates = pd.DatetimeIndex(prices.index)
    event_map = {
        pd.Timestamp(date): targets.iloc[index].dropna().clip(lower=0.0)
        for index, date in enumerate(event_dates)
    }
    holdings: dict[str, float] = {}
    cash = 1.0
    previous_prices: dict[str, float] = {}
    values: list[float] = []
    for date in dates:
        row = prices.loc[date]
        for symbol in list(holdings):
            price = float(row.get(symbol, np.nan))
            prior = previous_prices.get(symbol)
            if prior is not None and np.isfinite(price) and price > 0.0 and prior > 0.0:
                holdings[symbol] *= price / prior
            if np.isfinite(price) and price > 0.0:
                previous_prices[symbol] = price
        nav = cash + sum(holdings.values())
        target = event_map.get(pd.Timestamp(date))
        if target is not None:
            target = target[target.gt(1e-12)]
            current_weights = {
                symbol: value / nav for symbol, value in holdings.items()
            } if nav > 0.0 else {}
            symbols = set(current_weights) | set(target.index)
            buys = sum(max(float(target.get(symbol, 0.0)) - current_weights.get(symbol, 0.0), 0.0) for symbol in symbols)
            sells = sum(max(current_weights.get(symbol, 0.0) - float(target.get(symbol, 0.0)), 0.0) for symbol in symbols)
            cost = nav * (
                buys * (float(commission) + float(slippage))
                + sells * (float(commission) + float(slippage) + float(stamp_duty))
            )
            nav = max(nav - cost, 0.0)
            holdings = {symbol: nav * float(weight) for symbol, weight in target.items()}
            cash = nav * max(0.0, 1.0 - float(target.sum()))
            previous_prices = {
                symbol: float(row[symbol])
                for symbol in holdings
                if symbol in row.index and np.isfinite(row[symbol]) and row[symbol] > 0.0
            }
        values.append(nav)
    return pd.Series(values, index=dates, name="nav")


def random_drop_simulation(
    close: pd.DataFrame,
    targets: pd.DataFrame,
    event_dates: list[pd.Timestamp],
    *,
    drop_frac: float = 0.20,
    n_iter: int = 1000,
    seed: int = 42,
    costs: dict[str, float] | None = None,
) -> np.ndarray:
    """Drop holdings independently at each rebalance and renormalize survivors."""
    rng = np.random.default_rng(seed)
    costs = costs or {}
    results = np.empty(int(n_iter), dtype=float)
    for iteration in range(int(n_iter)):
        sampled = targets.copy()
        for row_number in range(len(sampled)):
            row = sampled.iloc[row_number]
            active = np.flatnonzero(row.to_numpy(dtype=float) > 1e-12)
            drop_count = min(len(active) - 1, max(1, int(round(len(active) * float(drop_frac)))))
            if drop_count <= 0:
                continue
            dropped = rng.choice(active, size=drop_count, replace=False)
            released_weight = float(sampled.iloc[row_number, dropped].sum())
            sampled.iloc[row_number, dropped] = 0.0
            survivors = np.setdiff1d(active, dropped, assume_unique=False)
            if len(survivors):
                sampled.iloc[row_number, survivors] += released_weight / len(survivors)
        nav = simulate_sparse_targets(close, sampled, event_dates, **costs)
        results[iteration] = _annual_return(nav)
    return results


def shifted_rebalance_simulation(
    close: pd.DataFrame,
    targets: pd.DataFrame,
    event_dates: list[pd.Timestamp],
    *,
    max_shift: int = 13,
    n_iter: int = 1000,
    seed: int = 43,
    costs: dict[str, float] | None = None,
) -> np.ndarray:
    """Shift each execution event by up to ``max_shift`` trading days."""
    rng = np.random.default_rng(seed)
    costs = costs or {}
    calendar = pd.DatetimeIndex(close.index)
    original = np.array([calendar.get_loc(date) for date in event_dates], dtype=int)
    results = np.empty(int(n_iter), dtype=float)
    for iteration in range(int(n_iter)):
        offsets = rng.integers(-int(max_shift), int(max_shift) + 1, size=len(original))
        shifted = np.clip(original + offsets, 0, len(calendar) - 1)
        for index in range(1, len(shifted)):
            shifted[index] = max(shifted[index], shifted[index - 1] + 1)
            shifted[index] = min(shifted[index], len(calendar) - len(shifted) + index)
        shifted_dates = [pd.Timestamp(calendar[index]) for index in shifted]
        nav = simulate_sparse_targets(close, targets, shifted_dates, **costs)
        results[iteration] = _annual_return(nav)
    return results


def block_bootstrap_returns(
    nav: pd.Series,
    rebalance_dates: list[pd.Timestamp],
    *,
    block_months: int = 3,
    n_iter: int = 1000,
    seed: int = 44,
) -> np.ndarray:
    """Moving-block bootstrap of rebalance-period returns."""
    sampled_nav = nav.reindex(pd.DatetimeIndex(rebalance_dates), method="ffill").dropna()
    periods = sampled_nav.pct_change().dropna().to_numpy(dtype=float)
    if len(periods) < int(block_months):
        raise ValueError("调仓周期数量不足以执行 Block Bootstrap")
    blocks = [periods[start:start + int(block_months)] for start in range(len(periods) - int(block_months) + 1)]
    rng = np.random.default_rng(seed)
    out = np.empty(int(n_iter), dtype=float)
    for iteration in range(int(n_iter)):
        draw: list[float] = []
        while len(draw) < len(periods):
            draw.extend(blocks[int(rng.integers(0, len(blocks)))])
        sequence = np.asarray(draw[: len(periods)], dtype=float)
        out[iteration] = float(np.prod(1.0 + sequence) ** (12.0 / len(sequence)) - 1.0)
    return out


def rolling_window_backtest(nav: pd.Series, window_years: int = 3) -> pd.DataFrame:
    daily = nav.astype(float).pct_change()
    years = sorted(set(nav.index.year))
    rows: list[dict[str, Any]] = []
    for start in years:
        end = start + int(window_years) - 1
        if end not in years:
            continue
        returns = daily.loc[(daily.index.year >= start) & (daily.index.year <= end)].dropna()
        if returns.empty:
            continue
        total = float((1.0 + returns).prod() - 1.0)
        path = (1.0 + returns).cumprod()
        std = float(returns.std(ddof=1))
        rows.append(
            {
                "start_year": start,
                "end_year": end,
                "annual_return": float((1.0 + total) ** (252.0 / len(returns)) - 1.0),
                "max_drawdown": float((path / path.cummax() - 1.0).min()),
                "sharpe": float(returns.mean() / std * math.sqrt(252.0)) if std > 0.0 else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _configure_font() -> None:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.is_file():
            font_manager.fontManager.addfont(str(path))
            plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=str(path)).get_name()]
            break
    plt.rcParams["axes.unicode_minus"] = False


def run(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config(args.config)
    tag = _safe_tag(args.tag)
    params = strategy_params(config)
    params.update(experiment_params(config, tag))
    backtest = config["backtest"]
    loader = VBTDataLoader(
        start_date=backtest["start_date"],
        end_date=backtest["end_date"],
        cache_enabled=False,
    )
    data = loader.load(
        factors=DEFAULT_FACTORS,
        include_prices=True,
        include_volumes=True,
        include_market_cap=True,
        include_float_mv=True,
        include_is_st=True,
        include_listed_date=True,
        include_absolute_financials=True,
        adjusted_prices=bool(backtest.get("adjusted_prices", True)),
    )
    targets, metadata = AblationStrategy("full", params).generate_signals(data)
    event_dates = [pd.Timestamp(date) for date in metadata["rebalance_dates"]]
    sparse = targets.loc[event_dates].fillna(0.0)
    active = sparse.abs().sum(axis=0).gt(0.0)
    sparse = sparse.loc[:, active]
    close = data["close"].reindex(columns=sparse.columns).ffill()
    costs = {
        "commission": float(backtest["commission"]),
        "stamp_duty": float(backtest["stamp_duty"]),
        "slippage": float(backtest["slippage"]),
    }
    output = ROOT / "output/robustness"
    output.mkdir(parents=True, exist_ok=True)
    base_nav_path = ROOT / config["output"]["directory"] / f"nav_series_{tag}.csv"
    if base_nav_path.is_file():
        base_frame = pd.read_csv(base_nav_path, index_col=0, parse_dates=True)
        base_nav = base_frame["full"].astype(float)
    else:
        base_nav = simulate_sparse_targets(close, sparse, event_dates, **costs)

    drops = random_drop_simulation(
        close, sparse, event_dates, drop_frac=args.drop_frac,
        n_iter=args.iterations, seed=args.seed, costs=costs,
    )
    existing_path = output / "monte_carlo_results.csv"
    existing = (
        pd.read_csv(existing_path)
        if args.reuse_existing_aux and existing_path.is_file()
        else pd.DataFrame()
    )
    if (
        len(existing) == int(args.iterations)
        and {"shifted_rebalance", "block_bootstrap"}.issubset(existing.columns)
    ):
        shifts = existing["shifted_rebalance"].to_numpy(dtype=float)
        blocks = existing["block_bootstrap"].to_numpy(dtype=float)
    else:
        shifts = shifted_rebalance_simulation(
            close, sparse, event_dates, max_shift=args.max_shift,
            n_iter=args.iterations, seed=args.seed + 1, costs=costs,
        )
        blocks = block_bootstrap_returns(
            base_nav, event_dates, block_months=args.block_months,
            n_iter=args.iterations, seed=args.seed + 2,
        )
    rolling = rolling_window_backtest(base_nav, window_years=3)
    rolling.to_csv(output / "rolling_window_results.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame({
        "random_drop": drops,
        "shifted_rebalance": shifts,
        "block_bootstrap": blocks,
    }).to_csv(output / "monte_carlo_results.csv", index=False, encoding="utf-8-sig")

    summaries = {
        "random_drop": _summary(drops),
        "shifted_rebalance": _summary(shifts),
        "block_bootstrap": _summary(blocks),
    }
    rolling_mean = float(rolling["annual_return"].mean())
    rolling_std = float(rolling["annual_return"].std(ddof=1))
    result = {
        "tag": tag,
        "iterations": int(args.iterations),
        "drop_frac": float(args.drop_frac),
        "max_shift_trading_days": int(args.max_shift),
        "block_months": int(args.block_months),
        **summaries,
        "rolling_window": {
            "mean": rolling_mean,
            "std": rolling_std,
            "min": float(rolling["annual_return"].min()),
            "max": float(rolling["annual_return"].max()),
        },
        "acceptance": {
            "random_drop_p05_gt_10pct": summaries["random_drop"]["p05"] > 0.10,
            "shifted_rebalance_p05_gt_10pct": summaries["shifted_rebalance"]["p05"] > 0.10,
            "block_bootstrap_p05_gt_10pct": summaries["block_bootstrap"]["p05"] > 0.10,
            "rolling_mean_gt_10pct": rolling_mean > 0.10,
            "rolling_std_lt_5pct": rolling_std < 0.05,
            "all_rolling_windows_gt_10pct": bool(rolling["annual_return"].gt(0.10).all()),
        },
    }
    (output / "monte_carlo_summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _configure_font()
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8), sharey=True)
    for ax, values, title in zip(
        axes,
        (drops, shifts, blocks),
        ("随机剔除持仓", "调仓日偏移 ±13 日", "3 个月 Block Bootstrap"),
    ):
        ax.hist(values, bins=35, color="#1976d2", alpha=0.82)
        ax.axvline(0.10, color="#c62828", linestyle="--", label="验收线 10%")
        ax.axvline(np.quantile(values, 0.05), color="#ef6c00", linestyle=":", label="5% 分位")
        ax.set_title(title)
        ax.xaxis.set_major_formatter(lambda value, _position: f"{value:.0%}")
        ax.grid(alpha=0.2)
    axes[0].set_ylabel("模拟次数")
    axes[-1].legend()
    fig.tight_layout()
    fig.savefig(output / "monte_carlo_distribution.png", dpi=180)
    plt.close(fig)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="红利低波策略稳健性压力测试")
    parser.add_argument("--tag", default="rollback_top_buffer")
    parser.add_argument("--config", default="config/vectorbt/ablation_config.yaml")
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--drop-frac", type=float, default=0.20)
    parser.add_argument("--max-shift", type=int, default=13)
    parser.add_argument("--block-months", type=int, default=3)
    parser.add_argument("--bootstrap", action="store_true", help="兼容旧命令；Block Bootstrap 默认执行")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--reuse-existing-aux",
        action="store_true",
        help="仅重算随机剔除，复用同迭代数的调仓偏移与Block Bootstrap结果",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.iterations <= 0:
        raise ValueError("iterations 必须大于 0")
    if not 0.0 < args.drop_frac < 1.0:
        raise ValueError("drop-frac 必须位于 (0, 1)")
    result = run(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"压力测试输出：{ROOT / 'output/robustness'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
