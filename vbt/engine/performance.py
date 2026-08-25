"""Standardized performance metrics and Markdown summary."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


class PerformanceCalculator:
    def __init__(self, results):
        self.results = results

    def compute_metrics(self) -> dict[str, float]:
        nav = self.results.nav.astype(float).dropna()
        returns = nav.pct_change().dropna()
        initial_capital = float(
            self.results.metadata.get("initial_capital", nav.iloc[0] if len(nav) else 0.0)
        )
        total_return = (
            float(nav.iloc[-1] / initial_capital - 1.0)
            if len(nav) and initial_capital > 0
            else 0.0
        )
        annual_return = (
            float((1.0 + total_return) ** (252.0 / (len(nav) - 1)) - 1.0)
            if len(nav) > 1
            else 0.0
        )
        max_drawdown = float((nav / nav.cummax() - 1.0).min()) if not nav.empty else 0.0
        sharpe = float("nan")
        if len(returns) > 1 and returns.std(ddof=1) > 0:
            sharpe = float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))
        gains = returns[returns > 0]
        losses = returns[returns < 0]
        profit_loss = (
            float(gains.mean() / abs(losses.mean())) if len(gains) and len(losses) else float("nan")
        )
        trades = self.results.trades
        avg_holding = float("nan")
        turnover = 0.0
        if isinstance(trades, pd.DataFrame) and not trades.empty:
            if "hold_days" in trades:
                values = pd.to_numeric(trades["hold_days"], errors="coerce").dropna()
                if not values.empty:
                    avg_holding = float(values.mean())
            elif {"date", "code", "side", "shares"}.issubset(trades.columns):
                positions: dict[str, float] = {}
                entries: dict[str, pd.Timestamp] = {}
                durations: list[int] = []
                ordered = trades.assign(date=pd.to_datetime(trades["date"])).sort_values("date")
                for trade in ordered.itertuples(index=False):
                    code = str(trade.code)
                    side = str(trade.side).strip().upper()
                    shares = float(trade.shares)
                    if side in {"BUY", "买入"}:
                        if positions.get(code, 0.0) <= 0:
                            entries[code] = pd.Timestamp(trade.date)
                        positions[code] = positions.get(code, 0.0) + shares
                    else:
                        entry = entries.get(code)
                        if entry is not None:
                            durations.append(max(0, int((pd.Timestamp(trade.date) - entry).days)))
                        positions[code] = positions.get(code, 0.0) - shares
                        if positions[code] <= 0:
                            entries.pop(code, None)
                if durations:
                    avg_holding = float(np.mean(durations))
            if "amount" in trades:
                turnover = float(pd.to_numeric(trades["amount"], errors="coerce").fillna(0).sum() / nav.mean())
            elif {"shares", "price"}.issubset(trades.columns):
                gross = (
                    pd.to_numeric(trades["shares"], errors="coerce").fillna(0).abs()
                    * pd.to_numeric(trades["price"], errors="coerce").fillna(0).abs()
                ).sum()
                turnover = float(gross / nav.mean())
        baseline_turnover = self.results.metadata.get("turnover")
        if baseline_turnover is not None:
            try:
                if np.isfinite(float(baseline_turnover)):
                    turnover = float(baseline_turnover)
            except (TypeError, ValueError):
                pass
        return {
            "total_return": total_return,
            "annual_return": annual_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "win_rate": float((returns > 0).mean()) if len(returns) else float("nan"),
            "profit_loss_ratio": profit_loss,
            "avg_holding_days": avg_holding,
            "turnover": turnover,
        }

    def generate_report(self) -> str:
        metrics = self.compute_metrics()
        lines = ["# VectorBT 回测绩效", ""]
        labels = {
            "total_return": "累计收益",
            "annual_return": "年化收益",
            "max_drawdown": "最大回撤",
            "sharpe_ratio": "夏普比率",
            "win_rate": "日胜率",
            "profit_loss_ratio": "盈亏比",
            "avg_holding_days": "平均持有天数",
            "turnover": "换手率",
        }
        for key, label in labels.items():
            value = metrics[key]
            if key in {"total_return", "annual_return", "max_drawdown", "win_rate"}:
                shown = "—" if not np.isfinite(value) else f"{value:.2%}"
            else:
                shown = "—" if not np.isfinite(value) else f"{value:.4f}"
            lines.append(f"- {label}：**{shown}**")
        return "\n".join(lines) + "\n"


__all__ = ["PerformanceCalculator"]
