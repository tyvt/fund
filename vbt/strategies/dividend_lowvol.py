"""Dividend-low-volatility strategy with an RQAlpha-aligned execution profile."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd

from vbt.strategies.base import BaseStrategy
from vbt.strategies.signal_generators import (
    apply_percentile_filters,
    compute_constrained_weight_by_factor,
    compute_equal_weight,
    compute_fusion_score,
    filter_volatility_band,
    filter_volatility_top,
    rank_by_factor,
    select_by_fusion_score,
    select_stocks_with_fallback,
)


def apply_hold_bonus(
    fusion_score: pd.Series,
    current_positions: Sequence[Any],
    bonus: float = 0.10,
) -> pd.Series:
    """Increase current holdings' scores without mutating the input series."""
    if float(bonus) < 0.0:
        raise ValueError("hold bonus must be non-negative")
    enhanced = fusion_score.astype(float).copy()
    symbols = [_position_symbol(position) for position in current_positions]
    active = enhanced.index.intersection(symbols)
    enhanced.loc[active] *= 1.0 + float(bonus)
    return enhanced


def should_trade(
    current_score: float,
    new_score: float,
    cost_estimate: float = 0.01,
) -> bool:
    """Only replace a holding when the score improvement clears trading cost."""
    values = (float(current_score), float(new_score), float(cost_estimate))
    if not all(np.isfinite(value) for value in values):
        return False
    if values[2] < 0.0:
        raise ValueError("cost estimate must be non-negative")
    return values[1] - values[0] > values[2]


def build_cost_aware_selection(
    scores: pd.DataFrame,
    *,
    hard_eligible: pd.DataFrame,
    candidate_n: int = 100,
    top_n: int = 20,
    hold_bonus: float = 0.10,
    cost_threshold: float = 0.01,
) -> tuple[pd.DataFrame, dict[str, dict[str, list[str]]]]:
    """Build a stateful target set using inertia and a replacement hurdle."""
    eligible = hard_eligible.reindex_like(scores).fillna(False).astype(bool)
    selected = pd.DataFrame(False, index=scores.index, columns=scores.columns)
    previous: list[str] = []
    trade_log: dict[str, dict[str, list[str]]] = {}
    candidate_limit = max(int(candidate_n), int(top_n))
    target = max(1, int(top_n))

    for day in scores.index:
        raw = scores.loc[day].where(eligible.loc[day]).dropna()
        candidate_scores = raw.nlargest(candidate_limit)
        candidate_set = set(str(symbol) for symbol in candidate_scores.index)
        incumbents = [
            symbol for symbol in previous if symbol in candidate_set and symbol in raw.index
        ]
        forced_sells = [symbol for symbol in previous if symbol not in incumbents]
        enhanced = apply_hold_bonus(candidate_scores, incumbents, hold_bonus)

        chosen = list(incumbents)
        newcomers = [str(symbol) for symbol in enhanced.sort_values(ascending=False).index
                     if str(symbol) not in chosen]
        while len(chosen) < target and newcomers:
            chosen.append(newcomers.pop(0))

        # A voluntary replacement must beat the bonus-adjusted weakest holding
        # by more than the explicit round-trip cost hurdle.
        for newcomer in newcomers:
            if not chosen:
                chosen.append(newcomer)
                continue
            weakest = min(chosen, key=lambda symbol: float(enhanced.get(symbol, -np.inf)))
            if should_trade(
                float(enhanced.get(weakest, np.nan)),
                float(enhanced.get(newcomer, np.nan)),
                cost_threshold,
            ):
                chosen.remove(weakest)
                chosen.append(newcomer)

        chosen = sorted(
            dict.fromkeys(chosen),
            key=lambda symbol: float(enhanced.get(symbol, -np.inf)),
            reverse=True,
        )[:target]
        selected.loc[day, chosen] = True
        sold = list(dict.fromkeys([*forced_sells, *(s for s in previous if s not in chosen)]))
        bought = [symbol for symbol in chosen if symbol not in previous]
        trade_log[pd.Timestamp(day).date().isoformat()] = {"sell": sold, "buy": bought}
        previous = chosen

    return selected, trade_log


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


def _position_symbol(position: Any) -> str:
    if isinstance(position, Mapping):
        symbol = position.get("symbol", position.get("code"))
        if symbol is None:
            raise ValueError("持仓或候选记录缺少 symbol/code")
        return str(symbol)
    return str(position)


def _position_number(position: Any, key: str, default: float = 0.0) -> float:
    if not isinstance(position, Mapping):
        return float(default)
    try:
        value = float(position.get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return value if np.isfinite(value) else float(default)


def apply_rebalance_buffer(
    current_positions: Sequence[Any],
    new_candidates: Sequence[Any],
    max_sell: int = 3,
    *,
    target_count: int | None = None,
) -> tuple[list[Any], dict[str, list[Any]]]:
    """Retain qualified holdings and enforce an absolute per-period sell cap.

    Candidate mappings may provide ``qualified`` (default ``True``), ``score``
    and ``score_decay``.  Deferred unqualified holdings remain in the portfolio
    when the sell cap has been reached; they are reconsidered next period.
    """
    limit = int(max_sell)
    if limit < 0:
        raise ValueError("max_sell 不能为负数")

    candidates = list(new_candidates)
    candidate_by_symbol = {_position_symbol(item): item for item in candidates}
    qualified_symbols = {
        symbol
        for symbol, item in candidate_by_symbol.items()
        if not isinstance(item, Mapping) or bool(item.get("qualified", True))
    }
    current = list(current_positions)
    if target_count is None:
        target = len(current) if current else len(candidates)
    else:
        target = max(0, int(target_count))

    qualified: list[Any] = []
    unqualified: list[Any] = []
    for position in current:
        if _position_symbol(position) in qualified_symbols:
            qualified.append(position)
        else:
            unqualified.append(position)

    def deterioration(position: Any) -> tuple[float, float, str]:
        decay = _position_number(position, "score_decay", 0.0)
        score = _position_number(position, "score", float("-inf"))
        return (-decay, score, _position_symbol(position))

    unqualified.sort(key=deterioration)
    sold = unqualified[:limit]
    sold_symbols = {_position_symbol(item) for item in sold}
    kept = qualified + unqualified[limit:]
    kept_symbols = {_position_symbol(item) for item in kept}
    buy_slots = max(0, target - len(kept))
    bought: list[Any] = []
    for candidate in candidates:
        symbol = _position_symbol(candidate)
        if symbol in kept_symbols or symbol in sold_symbols:
            continue
        bought.append(candidate)
        kept_symbols.add(symbol)
        if len(bought) >= buy_slots:
            break

    final_positions = kept + bought
    return final_positions, {"sell": sold, "buy": bought}


def build_rebalance_hold_eligible(
    volatility: pd.DataFrame,
    dividend: pd.DataFrame,
    *,
    hard_eligible: pd.DataFrame,
    volatility_mode: str,
    volatility_top_n: int = 10,
    dividend_top_n: int = 10,
    hold_threshold_multiplier: int = 3,
    volatility_mask: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return the conditional-retention pool for the configured low-vol mode.

    In Top mode, a holding remains qualified only when it is simultaneously in
    the lowest-volatility expanded pool and the highest-dividend expanded pool.
    Band/threshold modes retain their existing volatility mask and apply the
    expanded dividend rank inside that eligible universe.
    """
    multiplier = int(hold_threshold_multiplier)
    if multiplier <= 0:
        raise ValueError("hold_threshold_multiplier 必须为正整数")
    hard = hard_eligible.reindex_like(volatility).fillna(False).astype(bool)
    mode = str(volatility_mode).lower()
    dividend_limit = int(dividend_top_n) * multiplier

    if mode == "top":
        volatility_limit = int(volatility_top_n) * multiplier
        vol_qualified = filter_volatility_top(
            volatility.where(hard), top_n=volatility_limit
        )
        dividend_ranks = rank_by_factor(
            dividend, ascending=False, mask=hard & dividend.notna()
        )
        return hard & vol_qualified & dividend_ranks.le(dividend_limit)

    if volatility_mask is None:
        raise ValueError(f"{mode} 模式必须提供 volatility_mask")
    vol_qualified = volatility_mask.reindex_like(volatility).fillna(False).astype(bool)
    hold_base = hard & vol_qualified & dividend.notna()
    dividend_ranks = rank_by_factor(dividend, ascending=False, mask=hold_base)
    return hold_base & dividend_ranks.le(dividend_limit)


def build_buffered_weights(
    factor: pd.DataFrame,
    *,
    desired_selected: pd.DataFrame,
    hold_eligible: pd.DataFrame,
    expansion_eligible: pd.DataFrame | None = None,
    max_sell: int = 3,
    target_weight: float = 1.0,
    max_weight: float | None = 0.08,
    industries: Mapping[str, str] | pd.Series | None = None,
    industry_max: float | None = None,
    total_mv: pd.DataFrame | None = None,
    small_cap_weight_max: float | None = None,
    max_holdings: int | None = None,
    allow_partial: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, dict[str, list[str]]]]:
    """Apply the stateful buffer and allocate every buffered cross-section."""
    scores = factor.astype(float)
    desired = desired_selected.reindex_like(scores).fillna(False).astype(bool)
    qualified = hold_eligible.reindex_like(scores).fillna(False).astype(bool)
    expansion = (
        expansion_eligible.reindex_like(scores).fillna(False).astype(bool)
        if expansion_eligible is not None
        else desired | qualified
    )
    selected = pd.DataFrame(False, index=scores.index, columns=scores.columns)
    weights = pd.DataFrame(0.0, index=scores.index, columns=scores.columns)
    trade_log: dict[str, dict[str, list[str]]] = {}
    previous: list[dict[str, Any]] = []

    for day in scores.index:
        row_scores = scores.loc[day]
        desired_symbols = list(
            row_scores.where(desired.loc[day]).sort_values(ascending=False).dropna().index
        )
        hold_symbols = list(
            row_scores.where(qualified.loc[day]).sort_values(ascending=False).dropna().index
        )
        expansion_symbols = list(
            row_scores.where(expansion.loc[day]).sort_values(ascending=False).dropna().index
        )
        ordered_symbols = list(
            dict.fromkeys([*desired_symbols, *hold_symbols, *expansion_symbols])
        )
        candidates = [
            {
                "symbol": str(symbol),
                "score": float(row_scores.loc[symbol]),
                "qualified": bool(qualified.loc[day, symbol]),
            }
            for symbol in ordered_symbols
        ]
        current: list[dict[str, Any]] = []
        for position in previous:
            symbol = str(position["symbol"])
            current_score = row_scores.get(symbol, np.nan)
            is_qualified = bool(
                symbol in qualified.columns and qualified.loc[day, symbol]
            )
            previous_score = _position_number(position, "score", 0.0)
            score = float(current_score) if np.isfinite(current_score) else float("-inf")
            current.append(
                {
                    "symbol": symbol,
                    "score": score,
                    "score_decay": (
                        max(0.0, previous_score - score) if is_qualified else float("inf")
                    ),
                }
            )

        # Use the current unconstrained target size rather than perpetually
        # replacing every supplemental name added by an earlier feasibility
        # expansion.  Qualified holdings are still never forced out; when an
        # extra holding later becomes unqualified, the portfolio can converge
        # back toward the 10-name start / cap-feasible target naturally.
        target_count = int(desired.loc[day].sum())
        final, trades = apply_rebalance_buffer(
            current,
            candidates,
            max_sell=max_sell,
            target_count=target_count,
        )
        final_symbols = list(dict.fromkeys(_position_symbol(item) for item in final))
        if max_holdings is not None:
            final_symbols = final_symbols[: int(max_holdings)]
        sold_symbol_set = {_position_symbol(item) for item in trades["sell"]}

        # The retained set can become infeasible under the 8%/industry/small-cap
        # caps.  Adding unsold desired names preserves the sell limit and restores
        # full investment without weakening any allocation constraint.
        supplemental_buys: list[str] = []
        while True:
            row_mask = pd.DataFrame(False, index=[day], columns=scores.columns)
            row_mask.loc[day, final_symbols] = True
            allocated = compute_constrained_weight_by_factor(
                scores.loc[[day]],
                selected=row_mask,
                max_weight=max_weight,
                industries=industries,
                industry_max=industry_max,
                total_mv=total_mv.loc[[day]] if total_mv is not None else None,
                small_cap_weight_max=small_cap_weight_max,
                target_weight=target_weight,
            ).iloc[0]
            if float(allocated.sum()) >= float(target_weight) - 1e-8:
                break
            if max_holdings is not None and len(final_symbols) >= int(max_holdings):
                if allow_partial:
                    break
                raise ValueError(
                    f"{pd.Timestamp(day).date()} 达到最大持仓数后仍无法满足满仓及权重约束"
                )
            addition = next(
                (
                    symbol
                    for symbol in ordered_symbols
                    if symbol not in final_symbols and symbol not in sold_symbol_set
                ),
                None,
            )
            if addition is None:
                if allow_partial:
                    break
                raise ValueError(
                    f"{pd.Timestamp(day).date()} 缓冲后持仓无法满足满仓及权重约束"
                )
            final_symbols.append(addition)
            supplemental_buys.append(str(addition))

        selected.loc[day, final_symbols] = True
        weights.loc[day] = allocated
        sold_symbols = [_position_symbol(item) for item in trades["sell"]]
        bought_symbols = [
            *[_position_symbol(item) for item in trades["buy"]],
            *supplemental_buys,
        ]
        trade_log[pd.Timestamp(day).date().isoformat()] = {
            "sell": sold_symbols,
            "buy": list(dict.fromkeys(bought_symbols)),
        }
        previous = [
            {
                "symbol": str(symbol),
                "score": (
                    float(row_scores.loc[symbol])
                    if np.isfinite(row_scores.loc[symbol])
                    else float("-inf")
                ),
            }
            for symbol in final_symbols
        ]

    return selected, weights, trade_log


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
        fusion_mode = bool(self.params.get("fusion_mode", False))
        fusion_factors = tuple(
            self.params.get(
                "fusion_factors",
                (
                    "dividend_yield", "volatility_60d", "roe_ttm", "fcf_ev",
                    "pe_industry_quantile", "reversal_10d",
                ),
            )
        )
        fusion_v2 = fusion_mode and len(fusion_factors) > 2
        required_factors = list(self.required_factors)
        if fusion_v2:
            required_factors.extend(fusion_factors)
            if bool(self.params.get("overheat_filter_enabled", True)):
                required_factors.append("reversal_5d")
        missing = [factor for factor in dict.fromkeys(required_factors) if factor not in data]
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

        overheat_excluded = pd.DataFrame(False, index=hard.index, columns=hard.columns)
        if fusion_v2 and bool(self.params.get("overheat_filter_enabled", True)):
            reversal_5d = _at_signal_dates(
                data["reversal_5d"], signal_dates, list(rebalance_index), columns
            )
            trailing_gain_5d = -reversal_5d
            boundary = trailing_gain_5d.where(hard).quantile(
                float(self.params.get("overheat_quantile", 0.95)), axis=1
            )
            overheat_excluded = trailing_gain_5d.gt(boundary, axis=0) & hard
            hard &= ~overheat_excluded

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
        volatility_config = dict(self.params.get("volatility_filter") or {})
        volatility_mode = (
            "fusion"
            if fusion_mode
            else str(volatility_config.get("mode", "threshold")).lower()
        )
        if fusion_mode:
            volatility_mask = volatility.notna()
        elif volatility_mode == "band":
            volatility_mask = filter_volatility_band(
                volatility.where(hard),
                lower_quantile=float(volatility_config.get("lower_quantile", 0.20)),
                upper_quantile=float(volatility_config.get("upper_quantile", 0.80)),
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
        hard_stage = hard
        volatility_stage = hard & volatility_mask
        dividend_stage = (
            volatility_stage
            & dividend.ge(float(self.params.get("dividend_yield_min", 0.03)))
        )
        factor_eligible = (
            dividend_stage
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
        constrained_eligible = hard if fusion_v2 else factor_eligible & roe_mask & debt_mask
        total_mv = (
            _at_signal_dates(data["total_mv"], signal_dates, list(rebalance_index), columns)
            if "total_mv" in data
            else None
        )
        industries = data["industry"] if "industry" in data else None
        dividend_config = dict(self.params.get("dividend_filter") or {})
        dividend_top_n = int(
            dividend_config.get("top_n", self.params.get("top_n", 10))
        )
        selection_factor = dividend
        relaxed_eligible = factor_eligible
        hard_fallback = hard & volatility_mask if volatility_mode == "band" else hard
        fusion_candidate_n = int(self.params.get("fusion_candidate_n", 100))
        max_holding = (
            int(self.params.get("max_holding", 20 if fusion_v2 else 13))
            if fusion_mode else None
        )
        if fusion_mode:
            if fusion_v2:
                factor_matrices = {
                    name: _at_signal_dates(
                        data[name], signal_dates, list(rebalance_index), columns
                    )
                    for name in fusion_factors
                }
                default_weights = {
                    name: 1.0 / len(fusion_factors) for name in fusion_factors
                }
                selection_factor = compute_fusion_score(
                    factor_matrices,
                    weights=dict(self.params.get("fusion_weights") or default_weights),
                    factors=list(fusion_factors),
                    directions=dict(self.params.get("fusion_directions") or {}),
                    min_valid_factors=int(
                        self.params.get("fusion_min_valid_factors", len(fusion_factors))
                    ),
                    mask=constrained_eligible,
                )
            else:
                selection_factor = compute_fusion_score(
                    dividend,
                    volatility,
                    dividend_weight=float(self.params.get("dividend_weight", 0.5)),
                    volatility_weight=float(self.params.get("volatility_weight", 0.5)),
                    mask=constrained_eligible,
                )
            primary = select_by_fusion_score(
                selection_factor, top_n=fusion_candidate_n
            )
            relaxed_eligible = constrained_eligible
            hard_fallback = hard & dividend.notna() & volatility.notna()
        else:
            primary = constrained_eligible
        rebalance_trades: dict[str, dict[str, list[str]]] = {}
        if fusion_v2:
            selected, rebalance_trades = build_cost_aware_selection(
                selection_factor,
                hard_eligible=primary,
                candidate_n=fusion_candidate_n,
                top_n=int(self.params.get("top_n", 20)),
                hold_bonus=float(self.params.get("hold_bonus", 0.10)),
                cost_threshold=float(self.params.get("cost_threshold", 0.01)),
            )
            weights = compute_equal_weight(
                selected,
                max_weight=float(self.params.get("max_single_weight", 0.08)),
            )
            fallback_tiers = pd.Series("fusion_v2", index=selection_factor.index)
        else:
            selected, weights, fallback_tiers = select_stocks_with_fallback(
                selection_factor,
                primary_eligible=primary,
                relaxed_eligible=relaxed_eligible,
                hard_eligible=hard_fallback,
                top_n=dividend_top_n,
                min_holdings=int(self.params.get("min_holdings", 10)),
                fallback_top_n=int(self.params.get("fallback_top_n", 100)),
                min_investment_pct=float(self.params.get("min_investment_pct", 1.0)),
                max_single_weight=float(self.params.get("max_single_weight", 0.08)),
                industries=industries,
                industry_max=float(self.params.get("industry_single_max", 0.20)),
                total_mv=total_mv,
                small_cap_weight_max=float(self.params.get("small_cap_weight_max", 0.40)),
                max_holdings=max_holding,
                allow_partial=fusion_mode,
            )
        buffer_config = dict(self.params.get("rebalance_buffer") or {})
        buffer_enabled = bool(buffer_config.get("enabled", False))
        if buffer_enabled and not fusion_v2:
            hold_multiplier = int(buffer_config.get("hold_threshold_multiplier", 3))
            if fusion_mode:
                hold_eligible = select_by_fusion_score(
                    selection_factor,
                    top_n=fusion_candidate_n * hold_multiplier,
                ) & constrained_eligible
            else:
                hold_eligible = build_rebalance_hold_eligible(
                    volatility,
                    dividend,
                    hard_eligible=hard,
                    volatility_mode=volatility_mode,
                    volatility_top_n=int(volatility_config.get("top_n", 10)),
                    dividend_top_n=dividend_top_n,
                    hold_threshold_multiplier=hold_multiplier,
                    volatility_mask=volatility_mask,
                )
            selected, weights, rebalance_trades = build_buffered_weights(
                selection_factor,
                desired_selected=selected,
                hold_eligible=hold_eligible,
                expansion_eligible=(constrained_eligible if fusion_mode else hard),
                max_sell=int(buffer_config.get("max_sell_per_month", 3)),
                target_weight=float(self.params.get("min_investment_pct", 1.0)),
                max_weight=float(self.params.get("max_single_weight", 0.08)),
                industries=industries,
                industry_max=float(self.params.get("industry_single_max", 0.20)),
                total_mv=total_mv,
                small_cap_weight_max=float(
                    self.params.get("small_cap_weight_max", 0.40)
                ),
                max_holdings=max_holding,
                allow_partial=fusion_mode,
            )
        selected_counts = weights.gt(1e-12).sum(axis=1).astype(int)
        selected_mask = weights.gt(1e-12)
        turnover_by_date = weights.diff().abs().sum(axis=1).mul(0.5)
        if len(turnover_by_date):
            turnover_by_date.iloc[0] = 0.0
        expanded_mask = selected_mask & ~primary.reindex_like(selected_mask).fillna(False)
        symbols_by_date = lambda mask: {
            date.date().isoformat(): [str(symbol) for symbol in mask.columns[mask.loc[date]]]
            for date in mask.index
        }
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
            "candidate_stage_counts": {
                "hard": {
                    date.date().isoformat(): int(value)
                    for date, value in hard_stage.sum(axis=1).items()
                },
                "after_vol": {
                    date.date().isoformat(): int(value)
                    for date, value in volatility_stage.sum(axis=1).items()
                },
                "after_div": {
                    date.date().isoformat(): int(value)
                    for date, value in dividend_stage.sum(axis=1).items()
                },
                "final": {
                    date.date().isoformat(): int(value)
                    for date, value in primary.sum(axis=1).items()
                },
                "holdings": {
                    date.date().isoformat(): int(value)
                    for date, value in selected_mask.sum(axis=1).items()
                },
            },
            "core_candidate_symbols": symbols_by_date(primary),
            "selected_symbols": symbols_by_date(selected_mask),
            "expanded_symbols": symbols_by_date(expanded_mask),
            "invested_weights": {
                date.date().isoformat(): float(value)
                for date, value in weights.sum(axis=1).items()
            },
            "turnover_by_date": {
                date.date().isoformat(): float(value)
                for date, value in turnover_by_date.items()
            },
            "fallback_tiers": {
                date.date().isoformat(): str(value) for date, value in fallback_tiers.items()
            },
            "average_holdings": float(selected_counts.mean()),
            "average_invested_weight": float(weights.sum(axis=1).mean()),
            "volatility_filter_mode": volatility_mode,
            "fusion_mode": fusion_mode,
            "fusion_v2": fusion_v2,
            "fusion_factors": list(fusion_factors) if fusion_v2 else [],
            "hold_bonus": float(self.params.get("hold_bonus", 0.10)) if fusion_v2 else 0.0,
            "cost_threshold": float(self.params.get("cost_threshold", 0.01)) if fusion_v2 else 0.0,
            "overheat_excluded_counts": {
                date.date().isoformat(): int(value)
                for date, value in overheat_excluded.sum(axis=1).items()
            },
            "fusion_candidate_n": fusion_candidate_n,
            "max_holding": max_holding,
            "rebalance_buffer_enabled": buffer_enabled and not fusion_v2,
            "rebalance_trades": rebalance_trades,
            "max_sells_per_rebalance": max(
                (len(trades["sell"]) for trades in rebalance_trades.values()),
                default=0,
            ),
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


__all__ = [
    "CompiledStrategy",
    "DividendLowVolStrategy",
    "apply_rebalance_buffer",
    "apply_hold_bonus",
    "should_trade",
    "build_cost_aware_selection",
    "build_rebalance_hold_eligible",
    "build_buffered_weights",
]
