"""Factor snapshot adapter and complete-rule context builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import gc
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping, Sequence

import pandas as pd

from factor_snapshot_loader import load_snapshots

from vbt.adapters.price_loader import PriceLoader


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FACTORS = (
    "dividend_yield",
    "volatility_60d",
    "beta_300",
    "roe",
    "debt_ratio",
    "roe_volatility",
)


@dataclass
class VBTData(MutableMapping[str, Any]):
    matrices: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    aligned_context: Any = None
    price_long: pd.DataFrame = field(default_factory=pd.DataFrame)
    dividend_records: pd.DataFrame = field(default_factory=pd.DataFrame)
    split_records: pd.DataFrame = field(default_factory=pd.DataFrame)
    tradable_dates: set[tuple[str, pd.Timestamp]] = field(default_factory=set)

    def __getitem__(self, key: str) -> Any:
        return self.matrices[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.matrices[key] = value

    def __delitem__(self, key: str) -> None:
        del self.matrices[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.matrices)

    def __len__(self) -> int:
        return len(self.matrices)


class VBTDataLoader:
    def __init__(
        self,
        snapshot_path: str | Path = ROOT / "data/parquet/factors/snapshots",
        *,
        price_path: str | Path = ROOT / "data/parquet/stock_daily",
        start_date: str | None = None,
        end_date: str | None = None,
        cache_enabled: bool = True,
        cache_dir: str | Path = ROOT / "cache/vectorbt",
        threads: int = 4,
    ):
        self.snapshot_path = Path(snapshot_path).resolve()
        self.start_date = start_date
        self.end_date = end_date
        self.cache_enabled = bool(cache_enabled)
        self.cache_dir = Path(cache_dir).resolve()
        self.price_loader = PriceLoader(price_path, threads=threads)
        self._cache: dict[tuple, VBTData] = {}
        self._aligned_cache: dict[tuple, VBTData] = {}

    def load(
        self,
        *,
        factors: Sequence[str] = DEFAULT_FACTORS,
        symbols: Sequence[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        include_prices: bool = True,
    ) -> VBTData:
        start = start or self.start_date
        end = end or self.end_date
        selected = tuple(dict.fromkeys(str(factor) for factor in factors))
        normalized_symbols = tuple(sorted({str(s).zfill(6) for s in symbols})) if symbols else ()
        key = (selected, normalized_symbols, start, end, bool(include_prices))
        if self.cache_enabled and key in self._cache:
            return self._cache[key]

        long = load_snapshots(
            symbols=list(normalized_symbols) or None,
            start=start,
            end=end,
            factors=list(selected),
            snapshot_root=self.snapshot_path,
        )
        matrices: dict[str, Any] = {}
        for factor in selected:
            matrices[factor] = (
                long.pivot(index="trade_date", columns="symbol", values=factor)
                .sort_index()
                .sort_index(axis=1)
                .astype("float32")
            )
        selected_symbols = list(normalized_symbols) or sorted(long["symbol"].unique().tolist())
        price_long = pd.DataFrame()
        if include_prices:
            price_long = self.price_loader.load_long(
                symbols=selected_symbols,
                start=start,
                end=end,
                fields=("close", "total_mv", "is_st", "amount", "volume"),
            )
            for field in ("close", "total_mv", "is_st"):
                matrices[field] = (
                    price_long.pivot(index="trade_date", columns="symbol", values=field)
                    .sort_index()
                    .sort_index(axis=1)
                    .astype("float32")
                )
        industry = self.price_loader.industry_table().set_index("code")["industry"]
        matrices["industry"] = industry.reindex(selected_symbols).fillna("未分类")
        result = VBTData(
            matrices=matrices,
            metadata={
                "start_date": str(start) if start else None,
                "end_date": str(end) if end else None,
                "symbols": selected_symbols,
                "factors": list(selected),
                "factor_units": {
                    "dividend_yield": "fraction",
                    "volatility_60d": "fraction",
                    "beta_300": "ratio",
                    "roe": "percentage_points",
                    "debt_ratio": "percentage_points",
                    "roe_volatility": "percentage_points",
                },
            },
            # 矩阵研究只需要宽表；不保留数千万行长表副本。
            price_long=pd.DataFrame(),
        )
        del long, price_long
        self.price_loader._long_cache.clear()
        gc.collect()
        if self.cache_enabled:
            self._cache[key] = result
        return result

    def load_aligned(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        rebalance_mode: str = "index_annual",
        rebalance_days: int = 30,
        prefetch_size: int = 150,
        warm_panels: bool = True,
        verbose: bool = False,
    ) -> VBTData:
        start = str(start or self.start_date)
        end = str(end or self.end_date)
        if start in {"None", ""} or end in {"None", ""}:
            raise ValueError("完整规则模式必须提供 start/end")
        key = (start, end, rebalance_mode, int(rebalance_days), int(prefetch_size))
        if self.cache_enabled and key in self._aligned_cache:
            return self._aligned_cache[key]

        from dividend_lowvol_rotation.backtest import (
            BacktestContext,
            KlineStore,
            _collect_candidate_codes,
        )
        from dividend_lowvol_rotation.config import BETA_BENCHMARK_CODE
        from dividend_lowvol_rotation.dividend import load_fhps_all_records
        from dividend_lowvol_rotation.rebalance_schedule import resolve_rebalance_dates
        from dividend_lowvol_rotation.risk_screening import (
            batch_load_risk_history,
            build_dividend_year_index,
        )

        calendar = list(self.price_loader.calendar(start, end))
        if not calendar:
            raise ValueError(f"交易日历在 {start} ~ {end} 为空")
        rebalance_dates = resolve_rebalance_dates(
            calendar,
            mode=rebalance_mode,
            rebalance_days=rebalance_days,
            entry_anchor=calendar[0],
        )
        records = load_fhps_all_records(refresh=False, backtest_start=start)
        empty_store = KlineStore(start, end, backtest_start=start, kline_fq=None)
        temporary = BacktestContext(
            start=start,
            end=end,
            records=records,
            calendar=calendar,
            store=empty_store,
            industry_df=pd.DataFrame(),
        )
        candidates = _collect_candidate_codes(
            records, rebalance_dates, prefetch_size, ctx=temporary
        )
        price_start = (pd.Timestamp(start) - timedelta(days=450)).date().isoformat()
        price_long = self.price_loader.load_long(
            symbols=candidates,
            start=price_start,
            end=end,
            fields=("close", "total_mv", "amount", "is_st", "volume"),
        )
        store = KlineStore(price_start, end, backtest_start=start, kline_fq=None)
        for code, group in price_long.groupby("symbol", sort=False):
            frame = group.sort_values("trade_date").rename(columns={"trade_date": "date"})
            store._store_kline(str(code), frame[["date", "close"]].reset_index(drop=True))
            market = frame[["date", "total_mv", "amount"]].reset_index(drop=True)
            store._market_fields[str(code)] = market
            store._total_mv[str(code)] = market[["date", "total_mv"]].copy()

        benchmark = self.price_loader.load_index_long(
            symbols=[BETA_BENCHMARK_CODE], start=price_start, end=end, fields=("close",)
        )
        if not benchmark.empty:
            bench = benchmark.rename(columns={"trade_date": "date"}).sort_values("date")
            store._store_kline(
                str(BETA_BENCHMARK_CODE), bench[["date", "close"]].reset_index(drop=True)
            )

        industry_all = self.price_loader.industry_table()
        industry_df = industry_all[industry_all["code"].isin(candidates)].copy()
        risk_hist = batch_load_risk_history(candidates, refresh=False)
        dividends = self.price_loader.dividend_records()
        splits = self.price_loader.split_records()
        context = BacktestContext(
            start=start,
            end=end,
            records=records,
            calendar=calendar,
            store=store,
            industry_df=industry_df,
            risk_hist=risk_hist,
            dividend_year_index=build_dividend_year_index(records),
            dividend_cash_records=dividends[dividends["code"].isin(candidates)].copy(),
            split_records=splits[splits["code"].isin(candidates)].copy(),
            market_pe_hist=pd.DataFrame(),
        )
        if warm_panels:
            context.warm_panel_cache(rebalance_dates, prefetch_size, verbose=verbose)

        close = (
            price_long.pivot(index="trade_date", columns="symbol", values="close")
            .reindex(calendar)
            .ffill()
            .sort_index(axis=1)
        )
        total_mv = (
            price_long.pivot(index="trade_date", columns="symbol", values="total_mv")
            .reindex(calendar)
            .ffill()
            .sort_index(axis=1)
        )
        is_st = (
            price_long.pivot(index="trade_date", columns="symbol", values="is_st")
            .reindex(calendar)
            .ffill()
            .sort_index(axis=1)
        )
        industry = industry_all.set_index("code")["industry"].reindex(close.columns).fillna("未分类")
        result = VBTData(
            matrices={
                "close": close,
                "total_mv": total_mv,
                "is_st": is_st,
                "industry": industry,
            },
            metadata={
                "start_date": start,
                "end_date": end,
                "symbols": list(close.columns),
                "candidate_count": len(candidates),
                "rebalance_dates": [date.date().isoformat() for date in rebalance_dates],
                "rebalance_mode": rebalance_mode,
                "source": "stock_daily",
            },
            aligned_context=context,
            price_long=price_long,
            dividend_records=context.dividend_cash_records,
            split_records=context.split_records,
            tradable_dates=self.price_loader.exact_trade_mask(price_long),
        )
        if self.cache_enabled:
            self._aligned_cache[key] = result
        return result

    def load_baseline_aligned(
        self,
        baseline_path: str | Path,
        *,
        initial_capital: float = 100000.0,
    ) -> VBTData:
        """Load a frozen RQAlpha order stream and replay it on stock_daily prices.

        This is the strict validation path: selection/orders come from the immutable
        RQAlpha baseline, while close/total_mv/is_st and industries still come from
        the confirmed read-only sources.
        """
        import json

        from scripts.verify_vectorbt_vs_rqalpha import ensure_portable_baseline
        from vbt.strategies.dividend_lowvol import CompiledStrategy

        source = Path(baseline_path)
        if not source.is_absolute():
            source = ROOT / source
        portable = ensure_portable_baseline(source)
        summary = json.loads((portable / "summary.json").read_text(encoding="utf-8"))
        start = str(summary["start_date"])
        end = str(summary["end_date"])
        rq_trades = pd.read_csv(portable / "trades.csv")
        trades = pd.DataFrame(
            {
                "date": pd.to_datetime(rq_trades["datetime"]).dt.normalize(),
                "code": rq_trades["order_book_id"].astype(str).str[:6],
                "side": rq_trades["side"].astype(str).str.upper(),
                "shares": pd.to_numeric(rq_trades["last_quantity"], errors="coerce").fillna(0).astype(int),
                "price": pd.to_numeric(rq_trades["last_price"], errors="coerce"),
                "fee": pd.to_numeric(rq_trades["commission"], errors="coerce").fillna(0.0),
                "tax": pd.to_numeric(rq_trades["tax"], errors="coerce").fillna(0.0),
            }
        )
        raw_positions = pd.read_csv(portable / "positions.csv")
        raw_positions["date"] = pd.to_datetime(raw_positions["date"]).dt.normalize()
        raw_positions["code"] = raw_positions["order_book_id"].astype(str).str[:6]
        holdings = raw_positions.rename(columns={"quantity": "shares", "last_price": "price"})[
            ["date", "code", "shares", "price", "market_value"]
        ]
        symbols = sorted(set(trades["code"]) | set(holdings["code"]))
        calendar = pd.DatetimeIndex(self.price_loader.calendar(start, end))
        price_long = self.price_loader.load_long(
            symbols=symbols,
            start=start,
            end=end,
            fields=("close", "total_mv", "amount", "is_st", "volume"),
        )
        matrices: dict[str, Any] = {}
        for field in ("close", "total_mv", "is_st"):
            matrices[field] = (
                price_long.pivot(index="trade_date", columns="symbol", values=field)
                .reindex(calendar)
                .ffill()
                .sort_index(axis=1)
            )
        industry_all = self.price_loader.industry_table()
        matrices["industry"] = (
            industry_all.set_index("code")["industry"].reindex(matrices["close"].columns).fillna("未分类")
        )

        quantity = (
            raw_positions.pivot_table(index="date", columns="code", values="quantity", aggfunc="last")
            .reindex(index=calendar, columns=symbols)
            .fillna(0.0)
        )
        signed = trades["shares"].where(trades["side"].eq("BUY"), -trades["shares"])
        trade_delta = (
            trades.assign(signed=signed)
            .pivot_table(index="date", columns="code", values="signed", aggfunc="sum")
            .reindex(index=calendar, columns=symbols)
            .fillna(0.0)
        )
        unexplained = quantity.diff().fillna(quantity.iloc[0]).sub(trade_delta)
        split_deltas = {
            (pd.Timestamp(day), str(code)): int(round(value))
            for day, row in unexplained.iterrows()
            for code, value in row.items()
            if abs(float(value)) >= 0.5
        }

        account = pd.read_csv(portable / "stock_account.csv", parse_dates=["date"]).set_index("date")
        account.index = pd.DatetimeIndex(account.index).normalize()
        cash = pd.to_numeric(account["cash"], errors="coerce").reindex(calendar).ffill()
        previous_cash = cash.shift(1).fillna(float(initial_capital))
        trade_cash = pd.Series(0.0, index=calendar)
        for trade in trades.itertuples(index=False):
            gross = float(trade.shares) * float(trade.price)
            delta = -gross - float(trade.fee) if trade.side == "BUY" else gross - float(trade.fee) - float(trade.tax)
            trade_cash.loc[pd.Timestamp(trade.date)] += delta
        cash_flows = cash.sub(previous_cash).sub(trade_cash).fillna(0.0)
        baseline_nav = pd.read_csv(portable / "portfolio.csv", parse_dates=["date"]).set_index("date")
        compiled = CompiledStrategy(
            nav=baseline_nav,
            trades=trades,
            holdings=holdings,
            stock_summary=pd.DataFrame(),
            metadata={
                **summary,
                "mode": "rqalpha_frozen_baseline",
                "baseline_path": str(source),
                "initial_capital": float(initial_capital),
            },
            dividend_taxes=pd.DataFrame(),
            cash_flows=cash_flows,
            split_deltas=split_deltas,
        )
        return VBTData(
            matrices=matrices,
            metadata={
                "start_date": start,
                "end_date": end,
                "symbols": symbols,
                "source": "stock_daily",
                "baseline_path": str(source),
                "compiled_baseline": compiled,
            },
            aligned_context="frozen_baseline",
            price_long=price_long,
            tradable_dates=self.price_loader.exact_trade_mask(price_long),
        )


__all__ = ["DEFAULT_FACTORS", "VBTData", "VBTDataLoader"]
