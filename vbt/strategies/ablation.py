"""Point-in-time strategy variants for the constraint ablation experiment."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from vbt.strategies.base import BaseStrategy
from vbt.strategies.dividend_lowvol import (
    build_buffered_weights,
    build_rebalance_hold_eligible,
)
from vbt.strategies.signal_generators import (
    apply_percentile_filters,
    compute_equal_weight,
    filter_volatility_band,
    filter_volatility_top,
    rank_by_factor,
    select_stocks_with_fallback,
)


STRATEGY_NAMES = ("baseline0", "baseline1", "baseline2", "full")
DIAGNOSTIC_STRATEGY_NAMES = ("dividend_yield_only",)
SUPPORTED_STRATEGY_NAMES = (*STRATEGY_NAMES, *DIAGNOSTIC_STRATEGY_NAMES)


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


def rebalance_pairs(
    index: pd.DatetimeIndex,
    *,
    freq: str = "M",
    month: int = 1,
    day: int = -1,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return month/quarter-end signals executed on the next trading day."""
    normalized_freq = str(freq).upper()
    if normalized_freq == "A":
        return annual_rebalance_pairs(index, month=month, day=day)
    if normalized_freq not in {"M", "Q"}:
        raise ValueError(f"不支持的调仓频率：{freq}")
    dates = pd.DatetimeIndex(index).sort_values().unique()
    periods = dates.to_period(normalized_freq)
    pairs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for _, locations in pd.Series(range(len(dates)), index=dates).groupby(periods):
        signal_position = int(locations.iloc[-1])
        execution_position = signal_position + 1
        if execution_position < len(dates):
            pairs.append(
                (pd.Timestamp(dates[signal_position]), pd.Timestamp(dates[execution_position]))
            )
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
        if name not in SUPPORTED_STRATEGY_NAMES:
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
        if self.name in {"baseline2", "full", "dividend_yield_only"} and "dividend_yield" not in data:
            missing.append("dividend_yield")
        if self.name == "full":
            missing.extend(field for field in self.required_full if field not in data)
        if missing:
            raise KeyError(f"{self.name} 缺少数据字段：{', '.join(dict.fromkeys(missing))}")

        close = data["close"].sort_index()
        columns = close.columns
        rebalance_freq = str(self.params.get("rebalance_freq", "A"))
        default_day = -1 if rebalance_freq.upper() in {"M", "Q"} else 15
        pairs = rebalance_pairs(
            pd.DatetimeIndex(close.index),
            freq=rebalance_freq,
            month=int(self.params.get("rebalance_month", 1)),
            day=int(self.params.get("rebalance_day", default_day)),
        )
        if not pairs:
            raise ValueError("回测区间内没有可用的调仓日")
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
            & float_mv.ge(float(self.params.get("min_float_mv", 500_000_000)))
            & amount.ge(float(self.params.get("min_daily_amount", 1_000_000)))
        )
        if bool(self.params.get("exclude_st", True)):
            hard &= is_st.fillna(1).eq(0)

        fallback_tiers = pd.Series("not_applicable", index=execution_dates, dtype=object)
        volatility_mode = "not_applicable"
        buffer_enabled = False
        rebalance_trades: dict[str, dict[str, list[str]]] = {}

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
            if self.name in {"baseline2", "dividend_yield_only"}:
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
                volatility_config = dict(self.params.get("volatility_filter") or {})
                volatility_mode = str(
                    volatility_config.get("mode", "threshold")
                ).lower()
                if volatility_mode == "band":
                    volatility_mask = filter_volatility_band(
                        volatility.where(hard),
                        lower_quantile=float(
                            volatility_config.get("lower_quantile", 0.20)
                        ),
                        upper_quantile=float(
                            volatility_config.get("upper_quantile", 0.80)
                        ),
                    )
                elif volatility_mode == "top":
                    volatility_mask = filter_volatility_top(
                        volatility.where(hard),
                        top_n=int(volatility_config.get("top_n", 10)),
                    )
                elif volatility_mode == "threshold":
                    volatility_mask = volatility.le(
                        float(self.params.get("volatility_60d_max", 0.25))
                    )
                else:
                    raise ValueError(f"不支持的波动率过滤模式：{volatility_mode}")
                factor_eligible = (
                    hard
                    & dividend.ge(float(self.params.get("dividend_yield_min", 0.03)))
                    & volatility_mask
                    & beta.ge(float(self.params.get("beta_low_min", 0.45)))
                    & beta.le(float(self.params.get("beta_high_max", 0.81)))
                    & roe_volatility.le(
                        float(self.params.get("roe_volatility_max", 0.15)) * 100.0
                    )
                )
                roe_mask = apply_percentile_filters(
                    absolute_roe.where(factor_eligible),
                    lower_pct=float(self.params.get("roe_percentile_min", 0.20)),
                    upper_pct=None,
                )
                debt_mask = apply_percentile_filters(
                    absolute_debt.where(factor_eligible),
                    lower_pct=None,
                    upper_pct=float(self.params.get("debt_ratio_percentile_max", 0.80)),
                )
                eligible = factor_eligible & roe_mask & debt_mask
                total_mv = _at_dates(
                    data["total_mv"], signal_dates, execution_dates, columns
                )
                dividend_config = dict(self.params.get("dividend_filter") or {})
                dividend_top_n = int(
                    dividend_config.get("top_n", self.params.get("top_n", 10))
                )
                selected, weights, fallback_tiers = select_stocks_with_fallback(
                    dividend,
                    primary_eligible=eligible,
                    relaxed_eligible=factor_eligible,
                    hard_eligible=(
                        hard & volatility_mask if volatility_mode == "band" else hard
                    ),
                    top_n=dividend_top_n,
                    min_holdings=int(self.params.get("min_holdings", 10)),
                    fallback_top_n=int(self.params.get("fallback_top_n", 100)),
                    min_investment_pct=float(
                        self.params.get("min_investment_pct", 1.0)
                    ),
                    max_single_weight=float(
                        self.params.get("max_single_weight", 0.08)
                    ),
                    industries=data["industry"],
                    industry_max=float(self.params.get("industry_single_max", 0.20)),
                    total_mv=total_mv,
                    small_cap_weight_max=float(
                        self.params.get("small_cap_weight_max", 0.40)
                    ),
                )
                buffer_config = dict(self.params.get("rebalance_buffer") or {})
                buffer_enabled = bool(buffer_config.get("enabled", False))
                rebalance_trades: dict[str, dict[str, list[str]]] = {}
                if buffer_enabled:
                    hold_eligible = build_rebalance_hold_eligible(
                        volatility,
                        dividend,
                        hard_eligible=hard,
                        volatility_mode=volatility_mode,
                        volatility_top_n=int(volatility_config.get("top_n", 10)),
                        dividend_top_n=dividend_top_n,
                        hold_threshold_multiplier=int(
                            buffer_config.get("hold_threshold_multiplier", 3)
                        ),
                        volatility_mask=volatility_mask,
                    )
                    selected, weights, rebalance_trades = build_buffered_weights(
                        dividend,
                        desired_selected=selected,
                        hold_eligible=hold_eligible,
                        expansion_eligible=hard,
                        max_sell=int(buffer_config.get("max_sell_per_month", 3)),
                        target_weight=float(
                            self.params.get("min_investment_pct", 1.0)
                        ),
                        max_weight=float(self.params.get("max_single_weight", 0.08)),
                        industries=data["industry"],
                        industry_max=float(
                            self.params.get("industry_single_max", 0.20)
                        ),
                        total_mv=total_mv,
                        small_cap_weight_max=float(
                            self.params.get("small_cap_weight_max", 0.40)
                        ),
                    )
        targets = pd.DataFrame(
            np.nan,
            index=close.index,
            columns=columns,
            dtype="float32",
        )
        targets.loc[execution_dates] = weights.to_numpy(dtype="float32")
        counts = weights.gt(1e-12).sum(axis=1).astype(int)
        eligible_counts = eligible.sum(axis=1).astype(int)
        return targets, {
            "mode": "ablation_matrix",
            "strategy": self.name,
            "signal_dates": [day.date().isoformat() for day in signal_dates],
            "rebalance_dates": [day.date().isoformat() for day in execution_dates],
            "holding_period": int(self.params.get("holding_period", 20)),
            "selected_counts": {
                day.date().isoformat(): int(value) for day, value in counts.items()
            },
            "eligible_counts": {
                day.date().isoformat(): int(value) for day, value in eligible_counts.items()
            },
            "average_holdings": float(counts.mean()),
            "average_invested_weight": float(weights.sum(axis=1).mean()),
            "fallback_tiers": {
                day.date().isoformat(): str(value)
                for day, value in fallback_tiers.items()
            },
            "turnover": _turnover(weights),
            "point_in_time": True,
            "volatility_filter_mode": volatility_mode,
            "rebalance_buffer_enabled": buffer_enabled,
            "rebalance_trades": rebalance_trades,
            "max_sells_per_rebalance": max(
                (len(trades["sell"]) for trades in rebalance_trades.values()),
                default=0,
            ),
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
    "DIAGNOSTIC_STRATEGY_NAMES",
    "SUPPORTED_STRATEGY_NAMES",
    "AblationStrategy",
    "annual_rebalance_pairs",
    "rebalance_pairs",
    "baseline0_strategy",
    "baseline1_strategy",
    "baseline2_strategy",
    "full_strategy",
]
