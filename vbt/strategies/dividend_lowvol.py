"""Dividend-low-volatility strategy with an RQAlpha-aligned execution profile."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

import numpy as np
import pandas as pd

from vbt.strategies.base import BaseStrategy
from vbt.strategies.signal_generators import (
    apply_beta_constraint,
    apply_industry_cap,
    apply_market_cap_cap,
    compute_equal_weight,
    compute_weight_by_factor,
    rank_by_factor,
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


def _rebalance_mask(index: pd.DatetimeIndex, params: Mapping[str, Any]) -> pd.Series:
    freq = str(params.get("rebalance_freq", "A")).upper()
    mode = str(params.get("rebalance_mode", "")).lower()
    selected = pd.Series(False, index=index)
    if index.empty:
        return selected
    if freq == "A" or ("rebalance_freq" not in params and mode == "index_annual"):
        month = int(params.get("rebalance_month", 1))
        day = int(params.get("rebalance_day", 15))
        selected.iloc[0] = True
        for year in sorted(set(index.year)):
            target = pd.Timestamp(year=year, month=month, day=day)
            candidates = index[index > target]
            if len(candidates):
                selected.loc[candidates[0]] = True
        return selected
    periods = index.to_period("M" if freq == "M" else "Q")
    selected.iloc[0] = True
    for _, locations in pd.Series(range(len(index)), index=index).groupby(periods):
        selected.iloc[int(locations.iloc[0])] = True
    return selected


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

        cache_fields = (
            "rebalance_freq",
            "rebalance_mode",
            "rebalance_month",
            "rebalance_day",
            "dividend_yield_min",
            "volatility_60d_max",
            "beta_max",
            "roe_min",
            "debt_ratio_max",
            "roe_volatility_max",
        )
        cache_key = (id(data), *(self.params.get(field) for field in cache_fields))
        cached = self._matrix_cache.get(cache_key)
        if cached is None:
            full_index = pd.DatetimeIndex(data["close"].index)
            mask_dates = _rebalance_mask(full_index, self.params)
            rebalance_index = full_index[mask_dates]
            # 横截面筛选只在实际调仓日有意义。先切片再执行排序和约束，避免参数
            # 扫描对全市场每个交易日重复计算同一类无用信号。
            dividend = data["dividend_yield"].reindex(rebalance_index)
            volatility = data["volatility_60d"].reindex(rebalance_index)
            beta = data["beta_300"].reindex(rebalance_index)
            # 源矩阵的 ROE / 负债率 / ROE 波动率单位是百分点；YAML 使用小数。
            roe_min = float(self.params.get("roe_min", 0.08)) * 100.0
            debt_max = float(self.params.get("debt_ratio_max", 0.70)) * 100.0
            roe_vol_max = float(self.params.get("roe_volatility_max", 0.15)) * 100.0
            mask = (
                dividend.ge(float(self.params.get("dividend_yield_min", 0.03)))
                & volatility.le(float(self.params.get("volatility_60d_max", 0.25)))
                & beta.le(float(self.params.get("beta_max", 1.2)))
                & data["roe"].reindex(rebalance_index).ge(roe_min)
                & data["debt_ratio"].reindex(rebalance_index).le(debt_max)
                & data["roe_volatility"].reindex(rebalance_index).le(roe_vol_max)
            )
            ranks = rank_by_factor(dividend, ascending=False, mask=mask)
            cached = (full_index, rebalance_index, dividend, beta, mask, ranks)
            self._matrix_cache[cache_key] = cached
        full_index, rebalance_index, dividend, beta, mask, ranks = cached
        selected = ranks.le(int(self.params.get("top_n", 10))) & mask
        selected_counts = selected.sum(axis=1).to_dict()
        candidate_columns = selected.columns[selected.any(axis=0)]
        selected = selected.loc[:, candidate_columns]
        dividend = dividend.loc[:, candidate_columns]
        beta = beta.loc[:, candidate_columns]
        if str(self.params.get("weight_scheme", "dividend_yield")) == "equal":
            weights = compute_equal_weight(
                selected, max_weight=float(self.params.get("max_single_weight", 0.08))
            )
        else:
            weights = compute_weight_by_factor(
                dividend,
                selected=selected,
                max_weight=float(self.params.get("max_single_weight", 0.08)),
            )
        weights = apply_beta_constraint(
            weights,
            beta,
            beta_min=self.params.get("beta_low_min"),
            beta_max=self.params.get("beta_high_max"),
        )
        if "total_mv" in data:
            weights = apply_market_cap_cap(
                weights,
                data["total_mv"].reindex(index=rebalance_index, columns=weights.columns),
                small_cap_weight_max=float(self.params.get("small_cap_weight_max", 0.40)),
            )
        if "industry" in data:
            weights = apply_industry_cap(
                weights,
                data["industry"],
                max_weight=float(self.params.get("industry_single_max", 0.20)),
            )
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
            "rebalance_dates": [date.date().isoformat() for date in rebalance_index],
            "selected_counts": selected_counts,
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
