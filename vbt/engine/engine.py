"""VectorBT portfolio execution for compiled production-rule orders."""

from __future__ import annotations

import math
import gc
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import vectorbt as vectorbt_lib

from vbt.config import configure_logging, reproducibility_snapshot


CASH_FLOW_COLUMN = "__CASH_FLOW__"


@dataclass
class BacktestResults:
    portfolio: Any
    nav: pd.Series
    positions: pd.DataFrame
    shares: pd.DataFrame
    trades: pd.DataFrame
    holdings: pd.DataFrame
    stock_summary: pd.DataFrame
    dividend_taxes: pd.DataFrame
    metadata: dict[str, Any]

    @property
    def initial_capital(self) -> float:
        value = self.metadata.get("initial_capital")
        if value is not None:
            return float(value)
        return float(self.nav.iloc[0]) if not self.nav.empty else 0.0

    @property
    def total_return(self) -> float:
        if self.nav.empty or self.initial_capital <= 0:
            return 0.0
        return float(self.nav.iloc[-1] / self.initial_capital - 1.0)

    @property
    def annual_return(self) -> float:
        if len(self.nav) <= 1:
            return 0.0
        return float((1.0 + self.total_return) ** (252.0 / (len(self.nav) - 1)) - 1.0)

    @property
    def max_drawdown(self) -> float:
        if self.nav.empty:
            return 0.0
        return float((self.nav / self.nav.cummax() - 1.0).min())

    @property
    def sharpe_ratio(self) -> float:
        returns = self.nav.pct_change().dropna()
        if len(returns) < 2 or returns.std(ddof=1) <= 0:
            return float("nan")
        return float(returns.mean() / returns.std(ddof=1) * math.sqrt(252.0))


class VBTEngine:
    def __init__(
        self,
        *,
        data,
        strategy,
        initial_capital: float = 100000.0,
        commission: float = 0.0000854,
        min_commission: float = 5.0,
        stamp_duty_before: float = 0.001,
        stamp_duty_after: float = 0.0005,
        slippage: float = 0.0,
        backtest_config: dict[str, Any] | None = None,
    ):
        self.data = data
        self.strategy = strategy
        self.initial_capital = float(initial_capital)
        self.commission = float(commission)
        self.min_commission = float(min_commission)
        self.stamp_duty_before = float(stamp_duty_before)
        self.stamp_duty_after = float(stamp_duty_after)
        self.slippage = float(slippage)
        self.backtest_config = dict(backtest_config or {})
        self.logger = configure_logging(self.backtest_config.get("log_path"))
        self._last_result: BacktestResults | None = None

    def with_strategy(self, strategy) -> "VBTEngine":
        return type(self)(
            data=self.data,
            strategy=strategy,
            initial_capital=self.initial_capital,
            commission=self.commission,
            min_commission=self.min_commission,
            stamp_duty_before=self.stamp_duty_before,
            stamp_duty_after=self.stamp_duty_after,
            slippage=self.slippage,
            backtest_config=self.backtest_config,
        )

    def run(self, *, verbose: bool = False, force: bool = False) -> BacktestResults:
        if self._last_result is not None and not force:
            return self._last_result
        self.logger.info(
            "backtest start=%s end=%s capital=%.2f params=%s",
            self.data.metadata.get("start_date"),
            self.data.metadata.get("end_date"),
            self.initial_capital,
            self.strategy.params,
        )
        if bool(self.strategy.params.get("alignment_mode", True)):
            compiled = self.strategy.compile_aligned(
                self.data, initial_capital=self.initial_capital, verbose=verbose
            )
            portfolio = self._replay_compiled(compiled)
            value = portfolio.value(group_by=True)
            if isinstance(value, pd.DataFrame):
                value = value.iloc[:, 0]
            value = value.astype(float).rename("nav")
            assets = portfolio.assets()
            if isinstance(assets, pd.Series):
                assets = assets.to_frame()
            assets = assets.drop(columns=[CASH_FLOW_COLUMN], errors="ignore")
            close = self.data["close"].reindex(index=assets.index, columns=assets.columns).ffill()
            asset_value = assets * close
            positions = asset_value.div(value.replace(0, np.nan), axis=0).fillna(0.0)
            metadata = dict(compiled.metadata)
            metadata["vectorbt_version"] = vectorbt_lib.__version__
            metadata["reproducibility"] = reproducibility_snapshot(
                self.strategy.params,
                self.backtest_config,
            )
            result = BacktestResults(
                portfolio=portfolio,
                nav=value,
                positions=positions,
                shares=assets,
                trades=compiled.trades,
                holdings=compiled.holdings,
                stock_summary=compiled.stock_summary,
                dividend_taxes=compiled.dividend_taxes,
                metadata=metadata,
            )
        else:
            weights, metadata = self.strategy.generate_signals(self.data)
            active = weights.fillna(0.0).abs().sum(axis=0).gt(0.0)
            weights = weights.loc[:, active]
            if weights.shape[1] == 0:
                raise ValueError("策略在回测区间内没有生成任何目标持仓")
            close = self.data["close"].reindex_like(weights).ffill()
            order_kwargs = dict(
                size=weights,
                size_type="targetpercent",
                fees=self.commission,
                slippage=self.slippage,
                init_cash=self.initial_capital,
                cash_sharing=True,
                group_by=True,
                call_seq="auto",
                freq="1D",
            )
            portfolio = vectorbt_lib.Portfolio.from_orders(
                close,
                **order_kwargs,
            )
            # ``from_orders`` accepts one fee rate per order but has no separate
            # sell-tax argument. Infer sell orders once, then replay with their
            # stamp duty as a fixed fee. This retains fractional target weights
            # while avoiding the invalid all-market minimum-commission model.
            if self.stamp_duty_before > 0 or self.stamp_duty_after > 0:
                readable = portfolio.orders.records_readable
                sells = readable[
                    readable["Side"].astype(str).str.casefold().eq("sell")
                ]
                if not sells.empty:
                    fixed_fees = pd.DataFrame(
                        0.0,
                        index=weights.index,
                        columns=weights.columns,
                        dtype="float32",
                    )
                    for order in sells.itertuples(index=False):
                        timestamp = pd.Timestamp(getattr(order, "Timestamp"))
                        column = str(getattr(order, "Column"))
                        gross = abs(float(getattr(order, "Size")) * float(getattr(order, "Price")))
                        fixed_fees.loc[timestamp, column] += gross * self._stamp_rate(timestamp)
                    del portfolio
                    gc.collect()
                    portfolio = vectorbt_lib.Portfolio.from_orders(
                        close,
                        fixed_fees=fixed_fees,
                        **order_kwargs,
                    )
                    metadata = dict(metadata)
                    metadata["stamp_duty_model"] = "sell_only_two_pass"
                    del fixed_fees
            value = portfolio.value(group_by=True)
            if isinstance(value, pd.DataFrame):
                value = value.iloc[:, 0]
            shares = portfolio.assets()
            positions = shares.mul(close).div(value, axis=0).fillna(0.0)
            result = BacktestResults(
                portfolio=portfolio,
                nav=value.rename("nav"),
                positions=positions,
                shares=shares,
                trades=portfolio.orders.records_readable,
                holdings=pd.DataFrame(),
                stock_summary=pd.DataFrame(),
                dividend_taxes=pd.DataFrame(),
                metadata=dict(metadata),
            )
            result.metadata.setdefault("initial_capital", self.initial_capital)
        self._last_result = result
        self.logger.info(
            "backtest complete total_return=%.6f annual_return=%.6f max_drawdown=%.6f trades=%d",
            result.total_return,
            result.annual_return,
            result.max_drawdown,
            len(result.trades),
        )
        return result

    def _stamp_rate(self, day: pd.Timestamp) -> float:
        return self.stamp_duty_before if day < pd.Timestamp("2023-08-28") else self.stamp_duty_after

    @staticmethod
    def _is_buy(side: Any) -> bool:
        return str(side).strip().upper() in {"BUY", "买入"}

    def _cash_and_split_events(self, compiled) -> tuple[pd.Series, dict[tuple[pd.Timestamp, str], int]]:
        index = pd.DatetimeIndex(self.data["close"].index)
        if compiled.cash_flows is not None:
            flows = compiled.cash_flows.reindex(index).fillna(0.0).astype(float)
            return flows, dict(compiled.split_deltas or {})
        flows = pd.Series(0.0, index=index)
        split_deltas: dict[tuple[pd.Timestamp, str], int] = {}
        trades = compiled.trades.copy()
        if not trades.empty:
            trades["date"] = pd.to_datetime(trades["date"]).dt.normalize()
        trade_days = {day: frame for day, frame in trades.groupby("date")} if not trades.empty else {}
        dividends = self.data.dividend_records.copy()
        if not dividends.empty:
            dividends["payable_date"] = pd.to_datetime(dividends["payable_date"]).dt.normalize()
            dividend_days = {
                day: frame for day, frame in dividends.dropna(subset=["payable_date"]).groupby("payable_date")
            }
        else:
            dividend_days = {}
        splits = self.data.split_records.copy()
        if not splits.empty:
            splits["ex_date"] = pd.to_datetime(splits["ex_date"]).dt.normalize()
            split_days = {day: frame for day, frame in splits.groupby("ex_date")}
        else:
            split_days = {}

        shares: dict[str, int] = {}
        buy_dates: dict[str, pd.Timestamp] = {}
        for day in index:
            for event in dividend_days.get(day, pd.DataFrame()).itertuples(index=False):
                code = str(event.code)
                quantity = int(shares.get(code, 0))
                bought = buy_dates.get(code)
                if quantity <= 0 or bought is None or bought >= day:
                    continue
                hold_days = int((day - bought).days)
                rate = 0.20 if hold_days <= 30 else (0.10 if hold_days <= 365 else 0.0)
                flows.loc[day] += float(event.cash_per_share) * quantity * (1.0 - rate)

            for event in split_days.get(day, pd.DataFrame()).itertuples(index=False):
                code = str(event.code)
                old = int(shares.get(code, 0))
                if old <= 0:
                    continue
                new = int(old * float(event.factor))
                if new > old:
                    split_deltas[(day, code)] = split_deltas.get((day, code), 0) + new - old
                    shares[code] = new

            day_trades = trade_days.get(day)
            if day_trades is None:
                continue
            for trade in day_trades.itertuples(index=False):
                code = str(trade.code)
                quantity = int(trade.shares)
                if self._is_buy(trade.side):
                    if int(shares.get(code, 0)) <= 0:
                        buy_dates[code] = day
                    shares[code] = int(shares.get(code, 0)) + quantity
                else:
                    remaining = int(shares.get(code, 0)) - quantity
                    if remaining <= 0:
                        shares.pop(code, None)
                        buy_dates.pop(code, None)
                    else:
                        shares[code] = remaining
        return flows, split_deltas

    def _replay_compiled(self, compiled):
        close = self.data["close"].copy().sort_index().ffill()
        flows, split_deltas = self._cash_and_split_events(compiled)
        close[CASH_FLOW_COLUMN] = 1.0
        size = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
        price = close.copy()
        fixed_fees = pd.DataFrame(0.0, index=close.index, columns=close.columns)
        desired_cash: dict[tuple[pd.Timestamp, str], float] = {}
        share_delta: dict[tuple[pd.Timestamp, str], float] = {
            key: float(value) for key, value in split_deltas.items()
        }

        trades = compiled.trades.copy()
        if not trades.empty:
            trades["date"] = pd.to_datetime(trades["date"]).dt.normalize()
            for trade in trades.itertuples(index=False):
                day = pd.Timestamp(trade.date).normalize()
                code = str(trade.code)
                if day not in close.index or code not in close.columns:
                    continue
                quantity = float(trade.shares)
                signed = quantity if self._is_buy(trade.side) else -quantity
                key = (day, code)
                share_delta[key] = share_delta.get(key, 0.0) + signed
                trade_price = float(trade.price)
                price.loc[day, code] = trade_price
                fee = float(getattr(trade, "fee", 0.0) or 0.0)
                if self._is_buy(trade.side):
                    cash_delta = -quantity * trade_price - fee
                else:
                    explicit_tax = getattr(trade, "tax", None)
                    stamp = (
                        float(explicit_tax)
                        if explicit_tax is not None and pd.notna(explicit_tax)
                        else quantity * trade_price * self._stamp_rate(day)
                    )
                    cash_delta = quantity * trade_price - fee - stamp
                desired_cash[key] = desired_cash.get(key, 0.0) + cash_delta

        for key, delta in share_delta.items():
            day, code = key
            if day not in close.index or code not in close.columns:
                continue
            px = float(price.loc[day, code])
            if not np.isfinite(px) or px <= 0:
                continue
            desired = desired_cash.get(key, 0.0)
            if key in split_deltas and key not in desired_cash:
                desired = 0.0
            if abs(delta) < 1e-12:
                flows.loc[day] += desired
                continue
            size.loc[day, code] = delta
            fixed_fees.loc[day, code] = -delta * px - desired

        for day, flow in flows.items():
            if abs(float(flow)) < 1e-12:
                continue
            size.loc[day, CASH_FLOW_COLUMN] = 0.001
            price.loc[day, CASH_FLOW_COLUMN] = 1.0
            fixed_fees.loc[day, CASH_FLOW_COLUMN] = -float(flow)

        return vectorbt_lib.Portfolio.from_orders(
            close,
            size=size,
            size_type="amount",
            direction="longonly",
            price=price,
            fixed_fees=fixed_fees,
            fees=0.0,
            slippage=0.0,
            init_cash=self.initial_capital,
            cash_sharing=True,
            group_by=True,
            call_seq="auto",
            allow_partial=False,
            raise_reject=True,
            update_value=True,
            freq="1D",
        )


__all__ = ["BacktestResults", "VBTEngine"]
