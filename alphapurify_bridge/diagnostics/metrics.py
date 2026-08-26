"""Stable factor metrics used by the local AlphaPurify bridge."""

from __future__ import annotations

from typing import Any

import duckdb
import numpy as np
import pandas as pd


def _valid_frame(
    frame: pd.DataFrame,
    factor_col: str,
    return_col: str,
    *,
    direction: int = 1,
) -> pd.DataFrame:
    required = {"trade_date", factor_col, return_col}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"指标输入缺少列：{', '.join(sorted(missing))}")
    if int(direction) not in {-1, 1}:
        raise ValueError("direction 必须为 1 或 -1")
    result = frame.loc[:, ["trade_date", factor_col, return_col]].copy()
    result["trade_date"] = pd.to_datetime(result["trade_date"]).dt.normalize()
    result["factor_value"] = pd.to_numeric(result[factor_col], errors="coerce") * int(direction)
    result["forward_return"] = pd.to_numeric(result[return_col], errors="coerce")
    result = result.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["trade_date", "factor_value", "forward_return"]
    )
    return result.loc[:, ["trade_date", "factor_value", "forward_return"]]


def compute_ic(
    frame: pd.DataFrame,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    *,
    method: str = "spearman",
    direction: int = 1,
    min_observations: int = 20,
) -> pd.Series:
    """Compute a cross-sectional daily Pearson IC or Spearman rank IC."""

    method = str(method).lower()
    if method not in {"pearson", "spearman"}:
        raise ValueError("method 必须为 pearson 或 spearman")
    valid = _valid_frame(frame, factor_col, return_col, direction=direction)
    if valid.empty:
        return pd.Series(dtype=float, name="ic")
    with duckdb.connect() as con:
        con.register("factor_input", valid)
        if method == "spearman":
            source = """
                SELECT trade_date,
                       rank() OVER (PARTITION BY trade_date ORDER BY factor_value) AS x,
                       rank() OVER (PARTITION BY trade_date ORDER BY forward_return) AS y
                FROM factor_input
            """
        else:
            source = "SELECT trade_date, factor_value AS x, forward_return AS y FROM factor_input"
        result = con.execute(
            f"""
                SELECT trade_date, corr(x, y) AS ic
                FROM ({source})
                GROUP BY trade_date
                HAVING count(*) >= ?
                ORDER BY trade_date
            """,
            [max(2, int(min_observations))],
        ).df()
    if result.empty:
        return pd.Series(dtype=float, name="ic")
    series = pd.Series(result["ic"].to_numpy(dtype=float), index=pd.to_datetime(result["trade_date"]), name="ic")
    return series.replace([np.inf, -np.inf], np.nan).dropna()


def compute_ir(ic: pd.Series | list[float] | np.ndarray) -> float:
    values = pd.Series(ic, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(values) < 2:
        return float("nan")
    std = float(values.std(ddof=1))
    return float(values.mean() / std) if std > 0 else float("nan")


def _rebalance_subset(frame: pd.DataFrame, frequency: str) -> tuple[pd.DataFrame, int]:
    frequency = str(frequency).upper()
    if frequency not in {"M", "Q", "D"}:
        raise ValueError("rebalance_freq 必须为 M、Q 或 D")
    if frequency == "D":
        return frame, 252
    periods = frame["trade_date"].dt.to_period(frequency)
    selected_dates = frame.groupby(periods, observed=True)["trade_date"].max()
    return frame[frame["trade_date"].isin(selected_dates)].copy(), 12 if frequency == "M" else 4


def _annualized_compound(values: pd.Series, periods_per_year: int) -> float:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return float("nan")
    gross = float((1.0 + clean).prod())
    if gross <= 0:
        return float("nan")
    return gross ** (float(periods_per_year) / len(clean)) - 1.0


def compute_quantile_return(
    frame: pd.DataFrame,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    *,
    n_quantiles: int = 10,
    direction: int = 1,
    rebalance_freq: str = "M",
    monotonicity_min_rank_corr: float = 0.80,
) -> dict[str, Any]:
    """Compute cross-sectional quantile returns on period-end rebalance dates."""

    n_quantiles = int(n_quantiles)
    if n_quantiles < 3:
        raise ValueError("n_quantiles 不能小于 3")
    valid = _valid_frame(frame, factor_col, return_col, direction=direction)
    if valid.empty:
        return {
            "mean_returns": [],
            "annualized_returns": [],
            "curve": [],
            "spread_curve": [],
            "spread_return": float("nan"),
            "monotonicity": False,
            "monotonicity_rank_corr": float("nan"),
        }
    sample, periods_per_year = _rebalance_subset(valid, rebalance_freq)
    sample["percentile"] = sample.groupby("trade_date", observed=True)["factor_value"].rank(
        method="first", pct=True
    )
    sample["quantile"] = np.ceil(sample["percentile"] * n_quantiles).clip(1, n_quantiles).astype(int)
    returns = (
        sample.groupby(["trade_date", "quantile"], observed=True)["forward_return"]
        .mean()
        .unstack("quantile")
        .reindex(columns=range(1, n_quantiles + 1))
        .sort_index()
    )
    curve = (1.0 + returns.fillna(0.0)).cumprod()
    mean_returns = returns.mean(axis=0)
    annualized = returns.apply(lambda values: _annualized_compound(values, periods_per_year), axis=0)
    spread = returns[n_quantiles] - returns[1]
    spread_curve = (1.0 + spread.fillna(0.0)).cumprod()
    finite = annualized.dropna()
    monotonicity_corr = (
        float(pd.Series(finite.values).corr(pd.Series(finite.index, dtype=float), method="spearman"))
        if len(finite) >= 3
        else float("nan")
    )
    monotonicity = bool(
        np.isfinite(monotonicity_corr) and monotonicity_corr >= float(monotonicity_min_rank_corr)
    )
    return {
        "mean_returns": [None if pd.isna(value) else float(value) for value in mean_returns],
        "annualized_returns": [None if pd.isna(value) else float(value) for value in annualized],
        "curve": [
            {
                "trade_date": index.date().isoformat(),
                **{f"q{int(column)}": float(value) for column, value in row.items()},
            }
            for index, row in curve.iterrows()
        ],
        "spread_curve": [
            {"trade_date": index.date().isoformat(), "value": float(value)}
            for index, value in spread_curve.items()
        ],
        "spread_return": _annualized_compound(spread, periods_per_year),
        "monotonicity": monotonicity,
        "monotonicity_rank_corr": monotonicity_corr,
        "rebalance_observations": int(len(returns)),
    }


def compute_factor_returns(
    frame: pd.DataFrame,
    factor_col: str = "factor_value",
    return_col: str = "forward_return",
    *,
    direction: int = 1,
    min_observations: int = 20,
) -> pd.Series:
    """Estimate daily standardized univariate factor-return attribution."""

    valid = _valid_frame(frame, factor_col, return_col, direction=direction)
    if valid.empty:
        return pd.Series(dtype=float, name="factor_return")
    with duckdb.connect() as con:
        con.register("factor_input", valid)
        result = con.execute(
            """
            WITH normalized AS (
                SELECT trade_date,
                       (factor_value - avg(factor_value) OVER (PARTITION BY trade_date))
                       / nullif(stddev_samp(factor_value) OVER (PARTITION BY trade_date), 0) AS exposure,
                       forward_return
                FROM factor_input
            )
            SELECT trade_date,
                   sum(exposure * forward_return) / nullif(sum(exposure * exposure), 0) AS factor_return
            FROM normalized
            GROUP BY trade_date
            HAVING count(*) >= ?
            ORDER BY trade_date
            """,
            [max(2, int(min_observations))],
        ).df()
    if result.empty:
        return pd.Series(dtype=float, name="factor_return")
    return pd.Series(
        result["factor_return"].to_numpy(dtype=float),
        index=pd.to_datetime(result["trade_date"]),
        name="factor_return",
    ).dropna()


def compute_histogram(values: pd.Series, bins: int = 30) -> dict[str, list[float] | list[int]]:
    clean = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return {"edges": [], "counts": []}
    counts, edges = np.histogram(clean.to_numpy(dtype=float), bins=max(5, int(bins)))
    return {"edges": edges.tolist(), "counts": counts.astype(int).tolist()}


__all__ = [
    "compute_factor_returns",
    "compute_histogram",
    "compute_ic",
    "compute_ir",
    "compute_quantile_return",
]
