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
        adjusted_price_path: str | Path = ROOT / "data/parquet/stock_daily_qfq",
        financial_history_path: str | Path = ROOT / "cache/dividend_lowvol/risk_hist_merged.csv",
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
        self.adjusted_price_loader = PriceLoader(adjusted_price_path, threads=threads)
        self.financial_history_path = Path(financial_history_path).resolve()
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
        include_volumes: bool = False,
        include_market_cap: bool = True,
        include_float_mv: bool = False,
        include_is_st: bool = True,
        include_listed_date: bool = False,
        include_absolute_financials: bool = False,
        adjusted_prices: bool = False,
    ) -> VBTData:
        start = start or self.start_date
        end = end or self.end_date
        selected = tuple(dict.fromkeys(str(factor) for factor in factors))
        normalized_symbols = tuple(sorted({str(s).zfill(6) for s in symbols})) if symbols else ()
        key = (
            selected,
            normalized_symbols,
            start,
            end,
            bool(include_prices),
            bool(include_volumes),
            bool(include_market_cap),
            bool(include_float_mv),
            bool(include_is_st),
            bool(include_listed_date),
            bool(include_absolute_financials),
            bool(adjusted_prices),
        )
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
            price_fields = ["close"]
            if include_volumes:
                price_fields.extend(["amount", "volume"])
            if include_market_cap:
                price_fields.append("total_mv")
            if include_float_mv:
                price_fields.append("float_mv")
            if include_is_st:
                price_fields.append("is_st")
            price_long = self.price_loader.load_long(
                symbols=selected_symbols,
                start=start,
                end=end,
                fields=tuple(price_fields),
            )
            if adjusted_prices:
                adjusted = self.adjusted_price_loader.load_long(
                    symbols=selected_symbols,
                    start=start,
                    end=end,
                    fields=("close",),
                ).rename(columns={"close": "adjusted_close"})
                price_long = price_long.merge(
                    adjusted,
                    on=["trade_date", "symbol"],
                    how="left",
                    validate="one_to_one",
                )
                price_long["close"] = price_long["adjusted_close"].combine_first(
                    price_long["close"]
                )
                price_long = price_long.drop(columns="adjusted_close")
            for field in price_fields:
                matrices[field] = (
                    price_long.pivot(index="trade_date", columns="symbol", values=field)
                    .sort_index()
                    .sort_index(axis=1)
                    .astype("float32")
                )
        if include_listed_date:
            securities = self.price_loader.security_table().set_index("code")
            matrices["listed_date"] = securities["listed_date"].reindex(selected_symbols)
            matrices["de_listed_date"] = securities["de_listed_date"].reindex(selected_symbols)
        if include_absolute_financials:
            if not self.financial_history_path.exists():
                raise FileNotFoundError(f"财务历史不存在：{self.financial_history_path}")
            financials = pd.read_csv(
                self.financial_history_path,
                dtype={"code": str},
                usecols=["code", "report_year", "roe_pct", "debt_ratio_pct"],
            )
            financials["code"] = financials["code"].str.replace(
                r"\.0$", "", regex=True
            ).str.zfill(6)
            financials = financials[financials["code"].isin(selected_symbols)].copy()
            financials["report_year"] = pd.to_numeric(
                financials["report_year"], errors="coerce"
            ).astype("Int64")
            financials["roe_pct"] = pd.to_numeric(financials["roe_pct"], errors="coerce")
            financials["debt_ratio_pct"] = pd.to_numeric(
                financials["debt_ratio_pct"], errors="coerce"
            )
            financials = financials.dropna(subset=["report_year"])
            financials["available_date"] = pd.to_datetime(
                (financials["report_year"] + 1).astype(str) + "-04-30",
                errors="coerce",
            )
            matrices["absolute_financials"] = (
                financials.sort_values(["code", "report_year"])
                .drop_duplicates(["code", "report_year"], keep="last")
                .reset_index(drop=True)
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
                "adjusted_prices": bool(adjusted_prices),
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

__all__ = ["DEFAULT_FACTORS", "VBTData", "VBTDataLoader"]
