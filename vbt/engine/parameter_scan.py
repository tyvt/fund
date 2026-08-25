"""Configuration-driven grid search for VectorBT strategies."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from joblib import Parallel, delayed

from vbt.engine.performance import PerformanceCalculator


@dataclass
class ScanResults:
    table: pd.DataFrame
    metric: str

    def best_params(self) -> dict[str, Any]:
        if self.table.empty:
            return {}
        row = self.table.iloc[0]
        values: dict[str, Any] = {}
        for name in self.table.attrs.get("param_names", []):
            value = row[name]
            values[name] = value.item() if isinstance(value, np.generic) else value
        return values

    def to_parquet(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.table.to_parquet(target, index=False)
        return target


class ParameterScan:
    def __init__(
        self,
        *,
        engine,
        param_grid: Mapping[str, Sequence[Any]],
        metric: str = "sharpe_ratio",
    ):
        self.engine = engine
        self.param_grid = {key: list(values) for key, values in param_grid.items()}
        self.metric = str(metric)

    def _combinations(self) -> list[dict[str, Any]]:
        names = list(self.param_grid)
        return [dict(zip(names, values)) for values in product(*(self.param_grid[n] for n in names))]

    def _evaluate(self, params: dict[str, Any]) -> dict[str, Any]:
        strategy = self.engine.strategy.with_params(params)
        result = self.engine.with_strategy(strategy).run()
        metrics = PerformanceCalculator(result).compute_metrics()
        return {**params, **metrics, "status": "ok", "error": None}

    def _simulate_targets(self, targets: pd.DataFrame) -> dict[str, Any]:
        """Simulate sparse target weights without allocating a dense order cube."""
        active = targets.fillna(0.0).abs().sum(axis=0).gt(0.0)
        targets = targets.loc[:, active]
        if targets.shape[1] == 0:
            raise ValueError("策略在回测区间内没有生成任何目标持仓")

        close = (
            self.engine.data["close"]
            .reindex(index=targets.index, columns=targets.columns)
            .ffill()
            .astype("float64")
        )
        target_rows = targets.notna().any(axis=1).to_numpy().nonzero()[0]
        if len(target_rows) == 0:
            raise ValueError("策略在回测区间内没有生成调仓信号")

        prices = close.to_numpy(copy=False)
        weights = targets.iloc[target_rows].fillna(0.0).to_numpy(dtype="float64", copy=False)
        shares = np.zeros(prices.shape[1], dtype="float64")
        cash = float(self.engine.initial_capital)
        nav = np.empty(prices.shape[0], dtype="float64")
        previous = 0
        gross_turnover = 0.0
        cost_rate = max(0.0, float(self.engine.commission) + float(self.engine.slippage))

        for target_index, position in enumerate(target_rows):
            interval_prices = np.nan_to_num(prices[previous : position + 1], nan=0.0)
            nav[previous : position + 1] = interval_prices @ shares + cash
            pre_trade_nav = float(nav[position])
            row_prices = prices[position]
            row_weights = np.where(
                np.isfinite(row_prices) & (row_prices > 0.0), weights[target_index], 0.0
            )
            row_weights = np.clip(row_weights, 0.0, None)
            weight_sum = float(row_weights.sum())
            if weight_sum > 1.0:
                row_weights /= weight_sum

            current_values = np.nan_to_num(row_prices, nan=0.0) * shares
            post_trade_nav = pre_trade_nav
            turnover = 0.0
            # Fee/slippage changes the capital on which target-percent orders are
            # based. Two fixed-point iterations are sufficient at basis-point rates.
            for _ in range(2):
                target_values = row_weights * post_trade_nav
                turnover = float(np.abs(target_values - current_values).sum())
                post_trade_nav = max(0.0, pre_trade_nav - turnover * cost_rate)
            target_values = row_weights * post_trade_nav
            turnover = float(np.abs(target_values - current_values).sum())
            gross_turnover += turnover

            shares = np.divide(
                target_values,
                row_prices,
                out=np.zeros_like(target_values),
                where=np.isfinite(row_prices) & (row_prices > 0.0),
            )
            cash = float(post_trade_nav - target_values.sum())
            nav[position] = post_trade_nav
            previous = position + 1

        if previous < len(nav):
            tail_prices = np.nan_to_num(prices[previous:], nan=0.0)
            nav[previous:] = tail_prices @ shares + cash

        nav_series = (
            pd.Series(nav, index=targets.index)
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        if nav_series.empty:
            raise ValueError("参数组合未产生有效净值")
        returns = nav_series.pct_change().dropna()
        total_return = float(nav_series.iloc[-1] / self.engine.initial_capital - 1.0)
        annual_return = (
            float((1.0 + total_return) ** (252.0 / (len(nav_series) - 1)) - 1.0)
            if len(nav_series) > 1 and total_return > -1.0
            else -1.0 if total_return <= -1.0 else 0.0
        )
        max_drawdown = float((nav_series / nav_series.cummax() - 1.0).min())
        std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
        sharpe = float(returns.mean() / std * np.sqrt(252.0)) if std > 0 else float("nan")
        gains = returns[returns > 0]
        losses = returns[returns < 0]
        mean_nav = float(nav_series.mean())
        return {
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "win_rate": float((returns > 0).mean()) if len(returns) else float("nan"),
            "profit_loss_ratio": (
                float(gains.mean() / abs(losses.mean()))
                if len(gains) and len(losses)
                else float("nan")
            ),
            "avg_holding_days": float("nan"),
            "turnover": float(gross_turnover / mean_nav) if mean_nav > 0.0 else float("nan"),
            "backend": "sparse_interval",
            "status": "ok",
            "error": None,
        }

    def _run_matrix_batch(
        self, combinations: list[dict[str, Any]], *, n_jobs: int = -1
    ) -> list[dict[str, Any]]:
        """Evaluate combinations using the sparse rebalance-interval simulator.

        A dense VectorBT order tensor grows with dates, symbols and combinations,
        despite orders only occurring on a few rebalance dates. This research path
        keeps constant-share intervals and only calculates sparse trades. The best
        combination must still be rerun by ``VBTEngine`` for formal validation.
        """
        def evaluate(params: dict[str, Any]) -> dict[str, Any]:
            try:
                targets, _ = self.engine.strategy.with_params(params).generate_signals(
                    self.engine.data
                )
                return {**params, **self._simulate_targets(targets)}
            except Exception as exc:
                return {
                    **params,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }

        # Threads share the read-only factor matrices and the strategy's rank cache,
        # avoiding the multi-gigabyte copies that Windows process workers would make.
        return Parallel(n_jobs=int(n_jobs), prefer="threads", batch_size=1)(
            delayed(evaluate)(params) for params in combinations
        )

    def run(self, n_jobs: int = -1) -> ScanResults:
        combinations = self._combinations()
        if not combinations:
            return ScanResults(pd.DataFrame(), self.metric)
        # The complete-rule context includes production caches and temporary
        # adapters and therefore cannot be shared across threads. Research scans
        # use the independent pure-matrix path.
        aligned = bool(self.engine.strategy.params.get("alignment_mode", True))
        workers = 1 if aligned else int(n_jobs)

        def safe_evaluate(params: dict[str, Any]) -> dict[str, Any]:
            try:
                return self._evaluate(params)
            except Exception as exc:  # One failed set must not discard the whole scan.
                return {**params, "status": "error", "error": f"{type(exc).__name__}: {exc}"}

        if not aligned:
            rows = self._run_matrix_batch(combinations, n_jobs=workers)
        else:
            rows = Parallel(n_jobs=workers, prefer="threads")(
                delayed(safe_evaluate)(params) for params in combinations
            )
        table = pd.DataFrame(rows)
        if self.metric in table:
            table = table.sort_values(self.metric, ascending=False, na_position="last")
        table = table.reset_index(drop=True)
        table.attrs["param_names"] = list(self.param_grid)
        return ScanResults(table, self.metric)


__all__ = ["ParameterScan", "ScanResults"]
