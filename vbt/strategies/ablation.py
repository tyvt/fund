"""Point-in-time strategy variants for the constraint ablation experiment."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from vbt.strategies.base import BaseStrategy
from vbt.strategies.signal_generators import (
    apply_industry_cap,
    apply_market_cap_cap,
    compute_equal_weight,
    compute_weight_by_factor,
    rank_by_factor,
)


STRATEGY_NAMES = ("baseline0", "baseline1", "baseline2", "full")


def annual_rebalance_pairs(
    index: pd.DatetimeIndex, *, month: int = 1, day: int = 15
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return ``(signal_date, execution_date)`` pairs without same-day signals."""
    dates = pd.DatetimeIndex(index).sort_values().unique()
    pairs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for year in sorted(set(dates.year)):
        target = pd.Timestamp(year=year, month=int(month), day=int(day))
        locations = np.flatnonzero(dates > target)
        if not len(locations):
            continue
        position = int(locations[0])
        if position == 0:
            continue
        pairs.append((pd.Timestamp(dates[position - 1]), pd.Timestamp(dates[position])))
    return pairs


def _at_dates(
    frame: pd.DataFrame,
    signal_dates: list[pd.Timestamp],
    execution_dates: list[pd.Timestamp],
    columns: pd.Index,
) -> pd.DataFrame:
    out = frame.reindex(index=signal_dates, columns=columns).copy()
    out.index = pd.DatetimeIndex(execution_dates)
    return out


def _absolute_financials(
    reports: pd.DataFrame,
    signal_dates: list[pd.Timestamp],
    execution_dates: list[pd.Timestamp],
    columns: pd.Index,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    roe = pd.DataFrame(np.nan, index=execution_dates, columns=columns, dtype="float32")
    debt = pd.DataFrame(np.nan, index=execution_dates, columns=columns, dtype="float32")
    if reports.empty:
        return roe, debt
    source = reports.copy()
    source["available_date"] = pd.to_datetime(source["available_date"], errors="coerce")
    source = source.dropna(subset=["available_date", "code"]).sort_values(
        ["available_date", "report_year"]
    )
    for signal_date, execution_date in zip(signal_dates, execution_dates):
        known = source[source["available_date"] <= signal_date]
        if known.empty:
            continue
        latest = known.drop_duplicates("code", keep="last").set_index("code")
        roe.loc[execution_date] = latest["roe_pct"].reindex(columns).to_numpy(dtype="float32")
        debt.loc[execution_date] = latest["debt_ratio_pct"].reindex(columns).to_numpy(
            dtype="float32"
        )
    return roe, debt


def _turnover(weights: pd.DataFrame) -> float:
    if len(weights) <= 1:
        return 0.0
    # One-way turnover; initial portfolio formation is not counted as recurring turnover.
    values = weights.diff().iloc[1:].abs().sum(axis=1).mul(0.5)
    return float(values.mean()) if len(values) else 0.0


class AblationStrategy(BaseStrategy):
    """One of the four controlled matrix variants in the ablation specification."""

    required_common = ("close", "amount", "float_mv", "is_st", "listed_date")
    required_full = (
        "dividend_yield",
        "volatility_60d",
        "beta_300",
        "roe_volatility",
        "total_mv",
        "industry",
        "absolute_financials",
    )

    def __init__(self, name: str, params: Mapping[str, Any] | None = None):
        if name not in STRATEGY_NAMES:
            raise ValueError(f"未知消融策略：{name}")
        super().__init__(params)
        self.name = name
        self.params["alignment_mode"] = False

    def with_params(self, overrides: Mapping[str, Any] | None = None):
        params = dict(self.params)
        params.update(dict(overrides or {}))
        return type(self)(self.name, params)

    def generate_signals(self, data):
        missing = [field for field in self.required_common if field not in data]
        if self.name in {"baseline2", "full"} and "dividend_yield" not in data:
            missing.append("dividend_yield")
        if self.name == "full":
            missing.extend(field for field in self.required_full if field not in data)
        if missing:
            raise KeyError(f"{self.name} 缺少数据字段：{', '.join(dict.fromkeys(missing))}")

        close = data["close"].sort_index()
        columns = close.columns
        pairs = annual_rebalance_pairs(
            pd.DatetimeIndex(close.index),
            month=int(self.params.get("rebalance_month", 1)),
            day=int(self.params.get("rebalance_day", 15)),
        )
        if not pairs:
            raise ValueError("回测区间内没有可用的年度调仓日")
        signal_dates = [pair[0] for pair in pairs]
        execution_dates = [pair[1] for pair in pairs]

        signal_close = _at_dates(close, signal_dates, execution_dates, columns)
        execution_close = close.reindex(index=execution_dates, columns=columns)
        tradable = signal_close.gt(0) & execution_close.gt(0)

        listed = pd.to_datetime(data["listed_date"], errors="coerce").reindex(columns)
        listed_days = pd.DataFrame(
            [
                (signal_date - listed).dt.days.to_numpy(dtype="float64")
                for signal_date in signal_dates
            ],
            index=execution_dates,
            columns=columns,
        )
        is_st = _at_dates(data["is_st"], signal_dates, execution_dates, columns)
        amount = _at_dates(data["amount"], signal_dates, execution_dates, columns)
        float_mv = _at_dates(data["float_mv"], signal_dates, execution_dates, columns)
        hard = (
            tradable
            & listed_days.ge(float(self.params.get("min_listed_days", 365)))
            & is_st.fillna(1).eq(0)
            & float_mv.ge(float(self.params.get("min_float_mv", 500_000_000)))
            & amount.ge(float(self.params.get("min_daily_amount", 10_000_000)))
        )

        if self.name == "baseline0":
            eligible = tradable
            selected = eligible
            weights = compute_equal_weight(selected)
        elif self.name == "baseline1":
            eligible = hard
            selected = eligible
            weights = compute_equal_weight(selected)
        else:
            dividend = _at_dates(
                data["dividend_yield"], signal_dates, execution_dates, columns
            )
            if self.name == "baseline2":
                eligible = hard & dividend.notna()
                ranks = rank_by_factor(dividend, ascending=False, mask=eligible)
                selected = eligible & ranks.le(int(self.params.get("top_n", 10)))
                weights = compute_equal_weight(selected)
            else:
                volatility = _at_dates(
                    data["volatility_60d"], signal_dates, execution_dates, columns
                )
                beta = _at_dates(data["beta_300"], signal_dates, execution_dates, columns)
                roe_volatility = _at_dates(
                    data["roe_volatility"], signal_dates, execution_dates, columns
                )
                absolute_roe, absolute_debt = _absolute_financials(
                    data["absolute_financials"],
                    signal_dates,
                    execution_dates,
                    columns,
                )
                eligible = (
                    hard
                    & dividend.ge(float(self.params.get("dividend_yield_min", 0.03)))
                    & volatility.le(float(self.params.get("volatility_60d_max", 0.25)))
                    & beta.ge(float(self.params.get("beta_low_min", 0.45)))
                    & beta.le(float(self.params.get("beta_high_max", 0.81)))
                    & absolute_roe.ge(float(self.params.get("roe_min", 0.08)) * 100.0)
                    & absolute_debt.le(
                        float(self.params.get("debt_ratio_max", 0.70)) * 100.0
                    )
                    & roe_volatility.le(
                        float(self.params.get("roe_volatility_max", 0.15)) * 100.0
                    )
                )
                ranks = rank_by_factor(dividend, ascending=False, mask=eligible)
                selected = eligible & ranks.le(int(self.params.get("top_n", 10)))
                weights = compute_weight_by_factor(
                    dividend,
                    selected=selected,
                    max_weight=float(self.params.get("max_single_weight", 0.08)),
                )
                total_mv = _at_dates(
                    data["total_mv"], signal_dates, execution_dates, columns
                )
                weights = apply_market_cap_cap(
                    weights,
                    total_mv,
                    small_cap_weight_max=float(
                        self.params.get("small_cap_weight_max", 0.40)
                    ),
                )
                weights = apply_industry_cap(
                    weights,
                    data["industry"],
                    max_weight=float(self.params.get("industry_single_max", 0.20)),
                )

        targets = pd.DataFrame(
            np.nan,
            index=close.index,
            columns=columns,
            dtype="float32",
        )
        targets.loc[execution_dates] = weights.to_numpy(dtype="float32")
        counts = selected.sum(axis=1).astype(int)
        eligible_counts = eligible.sum(axis=1).astype(int)
        return targets, {
            "mode": "ablation_matrix",
            "strategy": self.name,
            "signal_dates": [day.date().isoformat() for day in signal_dates],
            "rebalance_dates": [day.date().isoformat() for day in execution_dates],
            "selected_counts": {
                day.date().isoformat(): int(value) for day, value in counts.items()
            },
            "eligible_counts": {
                day.date().isoformat(): int(value) for day, value in eligible_counts.items()
            },
            "average_holdings": float(counts.mean()),
            "average_invested_weight": float(weights.sum(axis=1).mean()),
            "turnover": _turnover(weights),
            "point_in_time": True,
        }


def baseline0_strategy(data, params):
    return AblationStrategy("baseline0", params).generate_signals(data)


def baseline1_strategy(data, params):
    return AblationStrategy("baseline1", params).generate_signals(data)


def baseline2_strategy(data, params):
    return AblationStrategy("baseline2", params).generate_signals(data)


def full_strategy(data, params):
    return AblationStrategy("full", params).generate_signals(data)


__all__ = [
    "STRATEGY_NAMES",
    "AblationStrategy",
    "annual_rebalance_pairs",
    "baseline0_strategy",
    "baseline1_strategy",
    "baseline2_strategy",
    "full_strategy",
]
