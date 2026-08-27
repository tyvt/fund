"""Shared period-metric helpers for fusion_v2 audit artefacts."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


def calculate_period_metrics(nav: pd.Series) -> dict[str, float]:
    clean = pd.to_numeric(nav, errors="coerce").dropna().astype(float)
    if len(clean) < 2 or float(clean.iloc[0]) <= 0.0:
        raise ValueError("绩效区间至少需要两个有效净值观测")
    returns = clean.pct_change().dropna()
    total_return = float(clean.iloc[-1] / clean.iloc[0] - 1.0)
    annual_return = float((1.0 + total_return) ** (252.0 / len(returns)) - 1.0)
    standard_deviation = float(returns.std(ddof=1))
    sharpe = (
        float(returns.mean() / standard_deviation * math.sqrt(252.0))
        if standard_deviation > 0.0 else float("nan")
    )
    return {
        "start": clean.index.min().date().isoformat(),
        "end": clean.index.max().date().isoformat(),
        "observations": int(len(clean)),
        "total_return": total_return,
        "annual_return": annual_return,
        "sharpe_ratio": sharpe,
        "max_drawdown": float((clean / clean.cummax() - 1.0).min()),
    }


def mean_turnover_for_years(values: dict[str, float], years: Iterable[int]) -> float:
    selected_years = set(int(year) for year in years)
    clean = [
        float(value)
        for day, value in values.items()
        if pd.Timestamp(day).year in selected_years and np.isfinite(float(value))
    ]
    return float(np.mean(clean)) if clean else float("nan")


__all__ = ["calculate_period_metrics", "mean_turnover_for_years"]
