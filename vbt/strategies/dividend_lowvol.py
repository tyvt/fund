"""Dividend-low-volatility strategy with an RQAlpha-aligned execution profile."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

import numpy as np
import pandas as pd

from vbt.strategies.base import BaseStrategy
from vbt.strategies.signal_generators import (
    apply_percentile_filters,
    select_stocks_with_fallback,
)


@dataclass
class CompiledStrategy:
    nav: pd.DataFrame
    trades: pd.DataFrame
    holdings: pd.DataFrame
    stock_summary: pd.DataFrame
    metadata: dict[str, Any]
    dividend_taxes: pd.DataFrame
    cash_flows: pd.Series | None = None
    split_deltas: dict[tuple[pd.Timestamp, str], int] | None = None


def _rebalance_pairs(
    index: pd.DatetimeIndex, params: Mapping[str, Any]
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Return look-ahead-safe ``(signal, execution)`` rebalance pairs."""
    freq = str(params.get("rebalance_freq", "A")).upper()
    dates = pd.DatetimeIndex(index).sort_values().unique()
    if dates.empty:
        return []
    signal_dates: list[pd.Timestamp] = []
    if freq == "A":
        month = int(params.get("rebalance_month", 1))
        day = int(params.get("rebalance_day", 15))
        for year in sorted(set(dates.year)):
            target = pd.Timestamp(year=year, month=month, day=day)
            locations = np.flatnonzero(dates > target)
            if len(locations) and int(locations[0]) > 0:
                signal_dates.append(pd.Timestamp(dates[int(locations[0]) - 1]))
    elif freq in {"M", "Q"}:
        periods = dates.to_period(freq)
        for _, locations in pd.Series(range(len(dates)), index=dates).groupby(periods):
            signal_dates.append(pd.Timestamp(dates[int(locations.iloc[-1])]))
    else:
        raise ValueError(f"不支持的调仓频率：{freq}")

    pairs: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for signal_date in signal_dates:
        position = int(dates.searchsorted(signal_date, side="right"))
        if position < len(dates):
            pairs.append((signal_date, pd.Timestamp(dates[position])))
    return pairs


def _at_signal_dates(
    frame: pd.DataFrame,
    signal_dates: list[pd.Timestamp],
    execution_dates: list[pd.Timestamp],
    columns: pd.Index,
) -> pd.DataFrame:
    out = frame.reindex(index=signal_dates, columns=columns).copy()
    out.index = pd.DatetimeIndex(execution_dates)
    return out


@contextmanager
def _stock_daily_suspension_adapter(data) -> Iterator[None]:
    """Make the existing rule engine use exact stock_daily availability for suspension."""
    from dividend_lowvol_rotation.rqalpha import rqalpha_bundle_prices

    original = rqalpha_bundle_prices.is_suspended_on_date

    def is_suspended(code: str, as_of, **_kwargs) -> bool:
        key = (str(code).split(".")[0].zfill(6), pd.Timestamp(as_of).normalize())
        return key not in data.tradable_dates

    rqalpha_bundle_prices.is_suspended_on_date = is_suspended
    try:
        yield
    finally:
        rqalpha_bundle_prices.is_suspended_on_date = original


class DividendLowVolStrategy(BaseStrategy):
    """Full production-rule strategy plus a pure-matrix research path."""

    required_factors = (
        "close",
        "amount",
        "float_mv",
        "is_st",
        "listed_date",
        "dividend_yield",
        "volatility_60d",
        "beta_300",
        "roe",
        "debt_ratio",
        "roe_volatility",
    )

    def __init__(self, params: Mapping[str, Any] | None = None):
        super().__init__(params)
        self._matrix_cache: dict[tuple[Any, ...], tuple[Any, ...]] = {}

    def with_params(self, overrides: Mapping[str, Any] | None = None):
        strategy = super().with_params(overrides)
        strategy._matrix_cache = self._matrix_cache
        return strategy

    def generate_signals(self, data):
        missing = [factor for factor in self.required_factors if factor not in data]
        if missing:
            if bool(self.params.get("alignment_mode", True)) and data.aligned_context is not None:
                compiled = self.compile_aligned(data)
                positions = self._positions_from_holdings(data["close"], compiled.holdings)
                return positions, compiled.metadata
            raise KeyError(f"策略缺少因子：{', '.join(missing)}")

        close = data["close"].sort_index()
        full_index = pd.DatetimeIndex(close.index)
        columns = close.columns
        pairs = _rebalance_pairs(full_index, self.params)
        if not pairs:
            raise ValueError("回测区间内没有可用的调仓日")
        signal_dates = [pair[0] for pair in pairs]
        rebalance_index = pd.DatetimeIndex(pair[1] for pair in pairs)

        signal_close = _at_signal_dates(close, signal_dates, list(rebalance_index), columns)
        execution_close = close.reindex(index=rebalance_index, columns=columns)
        tradable = signal_close.gt(0) & execution_close.gt(0)
        listed = pd.to_datetime(data["listed_date"], errors="coerce").reindex(columns)
        listed_days = pd.DataFrame(
            [(day - listed).dt.days.to_numpy(dtype="float64") for day in signal_dates],
            index=rebalance_index,
            columns=columns,
        )
        is_st = _at_signal_dates(data["is_st"], signal_dates, list(rebalance_index), columns)
        amount = _at_signal_dates(data["amount"], signal_dates, list(rebalance_index), columns)
        float_mv = _at_signal_dates(data["float_mv"], signal_dates, list(rebalance_index), columns)
        hard = (
            tradable
            & listed_days.ge(float(self.params.get("min_listed_days", 365)))
            & float_mv.ge(float(self.params.get("min_float_mv", 500_000_000)))
            & amount.ge(float(self.params.get("min_daily_amount", 1_000_000)))
        )
        if bool(self.params.get("exclude_st", True)):
            hard &= is_st.fillna(1).eq(0)

        dividend = _at_signal_dates(
            data["dividend_yield"], signal_dates, list(rebalance_index), columns
        )
        volatility = _at_signal_dates(
            data["volatility_60d"], signal_dates, list(rebalance_index), columns
        )
        beta = _at_signal_dates(data["beta_300"], signal_dates, list(rebalance_index), columns)
        roe = _at_signal_dates(data["roe"], signal_dates, list(rebalance_index), columns)
        debt = _at_signal_dates(data["debt_ratio"], signal_dates, list(rebalance_index), columns)
        roe_volatility = _at_signal_dates(
            data["roe_volatility"], signal_dates, list(rebalance_index), columns
        )
        factor_eligible = (
            hard
            & dividend.ge(float(self.params.get("dividend_yield_min", 0.03)))
            & volatility.le(float(self.params.get("volatility_60d_max", 0.25)))
            & beta.ge(float(self.params.get("beta_low_min", 0.45)))
            & beta.le(float(self.params.get("beta_high_max", 0.81)))
            & roe_volatility.le(float(self.params.get("roe_volatility_max", 0.15)) * 100.0)
        )
        roe_mask = apply_percentile_filters(
            roe.where(factor_eligible),
            lower_pct=float(self.params.get("roe_percentile_min", 0.20)),
            upper_pct=None,
        )
        debt_mask = apply_percentile_filters(
            debt.where(factor_eligible),
            lower_pct=None,
            upper_pct=float(self.params.get("debt_ratio_percentile_max", 0.80)),
        )
        primary = factor_eligible & roe_mask & debt_mask
        total_mv = (
            _at_signal_dates(data["total_mv"], signal_dates, list(rebalance_index), columns)
            if "total_mv" in data
            else None
        )
        industries = data["industry"] if "industry" in data else None
        selected, weights, fallback_tiers = select_stocks_with_fallback(
            dividend,
            primary_eligible=primary,
            relaxed_eligible=factor_eligible,
            hard_eligible=hard,
            top_n=int(self.params.get("top_n", 10)),
            min_holdings=int(self.params.get("min_holdings", 10)),
            fallback_top_n=int(self.params.get("fallback_top_n", 100)),
            min_investment_pct=float(self.params.get("min_investment_pct", 1.0)),
            max_single_weight=float(self.params.get("max_single_weight", 0.08)),
            industries=industries,
            industry_max=float(self.params.get("industry_single_max", 0.20)),
            total_mv=total_mv,
            small_cap_weight_max=float(self.params.get("small_cap_weight_max", 0.40)),
        )
        selected_counts = weights.gt(1e-12).sum(axis=1).astype(int)
        active = weights.abs().sum(axis=0).gt(0.0)
        weights = weights.loc[:, active]
        targets = pd.DataFrame(
            np.nan,
            index=full_index,
            columns=weights.columns,
            dtype="float32",
        )
        targets.loc[rebalance_index] = weights.to_numpy(dtype="float32")
        return targets, {
            "mode": "matrix",
            "signal_dates": [date.date().isoformat() for date in signal_dates],
            "rebalance_dates": [date.date().isoformat() for date in rebalance_index],
            "holding_period": int(self.params.get("holding_period", 20)),
            "selected_counts": {
                date.date().isoformat(): int(value) for date, value in selected_counts.items()
            },
            "eligible_counts": {
                date.date().isoformat(): int(value)
                for date, value in primary.sum(axis=1).items()
            },
            "fallback_tiers": {
                date.date().isoformat(): str(value) for date, value in fallback_tiers.items()
            },
            "average_holdings": float(selected_counts.mean()),
            "average_invested_weight": float(weights.sum(axis=1).mean()),
        }

    def compile_aligned(
        self,
        data,
        *,
        initial_capital: float = 100000.0,
        verbose: bool = False,
    ) -> CompiledStrategy:
        frozen = data.metadata.get("compiled_baseline")
        if frozen is not None:
            return frozen
        if data.aligned_context is None:
            raise ValueError("完整规则模式需要 VBTDataLoader.load_aligned() 返回的数据")
        from dividend_lowvol_rotation.backtest import run_backtest
        from dividend_lowvol_rotation.strategy_params import StrategyParams

        top_n = int(self.params.get("top_n", 10))
        # 对齐模式默认只覆盖 Top N，其余阈值继续由生产策略的 config 动态
        # 解析。研究/扫描如需覆盖原生规则，必须显式写入 alignment_overrides，
        # 避免矩阵研究参数无意改变 RQAlpha 对齐口径。
        native_overrides = dict(self.params.get("alignment_overrides") or {})
        native_overrides.setdefault("top_n", top_n)
        native = StrategyParams(**native_overrides)
        mode = str(self.params.get("rebalance_mode", "index_annual"))
        if "rebalance_mode" not in self.params:
            freq = str(self.params.get("rebalance_freq", "A")).upper()
            mode = {"M": "monthly", "Q": "quarterly_report"}.get(freq, "index_annual")
        with _stock_daily_suspension_adapter(data):
            nav, trades, holdings, stock_summary, metadata, taxes = run_backtest(
                start=data.metadata["start_date"],
                end=data.metadata["end_date"],
                top_n=top_n,
                rebalance_days=int(self.params.get("rebalance_days", 30)),
                initial_capital=float(initial_capital),
                prefetch_size=int(self.params.get("prefetch_size", 150)),
                verbose=verbose,
                ctx=data.aligned_context,
                strategy_params=native,
                rebalance_mode=mode,
                sell_mode=str(self.params.get("sell_mode", "index_rules")),
            )
        metadata = dict(metadata)
        metadata.update(
            {
                "mode": "rqalpha_aligned",
                "data_source": "stock_daily",
                "strategy_params": dict(self.params),
                "native_overrides": native.to_dict(),
                "initial_capital": float(initial_capital),
            }
        )
        return CompiledStrategy(nav, trades, holdings, stock_summary, metadata, taxes)

    @staticmethod
    def _positions_from_holdings(close: pd.DataFrame, holdings: pd.DataFrame) -> pd.DataFrame:
        positions = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        if holdings.empty:
            return positions
        snapshots = holdings.copy()
        snapshots["date"] = pd.to_datetime(snapshots["date"])
        for day, group in snapshots.groupby("date"):
            if day not in positions.index:
                continue
            market_values = group.set_index("code")["market_value"].astype(float)
            total = float(market_values.sum())
            if total > 0:
                common = market_values.index.intersection(positions.columns)
                positions.loc[day, common] = (market_values / total).reindex(common)
        return positions.mask(positions.eq(0.0)).ffill().fillna(0.0).astype(float)


__all__ = ["CompiledStrategy", "DividendLowVolStrategy"]
