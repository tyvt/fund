"""Read-only adapters for prices, calendar, industries, and corporate actions."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence

import duckdb
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICE_ROOT = ROOT / "data/parquet/stock_daily"
DEFAULT_INDEX_ROOT = ROOT / "data/parquet/index_daily"
DEFAULT_CALENDAR_PATH = ROOT / "data/parquet/trade_calendar/calendar.parquet"
DEFAULT_INDUSTRY_PATH = ROOT / "cache/dividend_lowvol/stock_industry_sw_l1.csv"
DEFAULT_DIVIDEND_PATH = ROOT / "data/parquet/stock_dividend/dividend_events.parquet"
DEFAULT_SPLIT_PATH = ROOT / "data/parquet/stock_split/split_events.parquet"

PRICE_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "turnover",
    "pct_chg",
    "total_mv",
    "float_mv",
    "is_st",
    "limit_up",
    "limit_down",
}


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _normalize_symbols(symbols: Sequence[str] | None) -> list[str] | None:
    if symbols is None:
        return None
    return sorted({str(symbol).strip().split(".")[0].zfill(6) for symbol in symbols})


class PriceLoader:
    """Query the local Parquet lake without writing into it."""

    def __init__(
        self,
        price_root: str | Path = DEFAULT_PRICE_ROOT,
        *,
        index_root: str | Path = DEFAULT_INDEX_ROOT,
        calendar_path: str | Path = DEFAULT_CALENDAR_PATH,
        industry_path: str | Path = DEFAULT_INDUSTRY_PATH,
        dividend_path: str | Path = DEFAULT_DIVIDEND_PATH,
        split_path: str | Path = DEFAULT_SPLIT_PATH,
        threads: int = 4,
    ):
        self.price_root = Path(price_root).resolve()
        self.index_root = Path(index_root).resolve()
        self.calendar_path = Path(calendar_path).resolve()
        self.industry_path = Path(industry_path).resolve()
        self.dividend_path = Path(dividend_path).resolve()
        self.split_path = Path(split_path).resolve()
        self.threads = max(1, int(threads))
        self._long_cache: dict[tuple, pd.DataFrame] = {}

    def _load_domain_long(
        self,
        root: Path,
        *,
        symbols: Sequence[str] | None,
        start: str | None,
        end: str | None,
        fields: Sequence[str],
    ) -> pd.DataFrame:
        selected = list(dict.fromkeys(str(field) for field in fields))
        unknown = sorted(set(selected) - PRICE_FIELDS)
        if unknown:
            raise ValueError(f"未知行情字段：{', '.join(unknown)}")
        normalized = _normalize_symbols(symbols)
        key = (str(root), tuple(normalized or ()), start, end, tuple(selected))
        cached = self._long_cache.get(key)
        if cached is not None:
            return cached.copy()

        parquet_glob = root / "year=*" / "*.parquet"
        if not root.exists():
            raise FileNotFoundError(f"行情目录不存在：{root}")
        params: list[object] = []
        predicates = ["try_cast(trade_date AS DATE) IS NOT NULL"]
        if start:
            predicates.append("try_cast(trade_date AS DATE) >= ?")
            params.append(str(start))
        if end:
            predicates.append("try_cast(trade_date AS DATE) <= ?")
            params.append(str(end))
        if normalized is not None:
            if not normalized:
                predicates.append("false")
            else:
                predicates.append("symbol IN (" + ",".join("?" for _ in normalized) + ")")
                params.extend(normalized)
        aggregates = ", ".join(f'max("{field}") AS "{field}"' for field in selected)
        projection = f", {aggregates}" if aggregates else ""
        query = f"""
            SELECT try_cast(trade_date AS DATE) AS trade_date,
                   cast(symbol AS VARCHAR) AS symbol{projection}
            FROM read_parquet('{_sql_path(parquet_glob)}', hive_partitioning=true, union_by_name=true)
            WHERE {' AND '.join(predicates)}
            GROUP BY 1, 2
            ORDER BY 1, 2
        """
        with duckdb.connect() as con:
            con.execute(f"SET threads TO {self.threads}")
            frame = con.execute(query, params).fetch_df()
        if not frame.empty:
            frame["trade_date"] = pd.to_datetime(frame["trade_date"])
            frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
        self._long_cache[key] = frame
        return frame.copy()

    def load_long(
        self,
        *,
        symbols: Sequence[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        fields: Sequence[str] = ("close", "total_mv", "is_st"),
    ) -> pd.DataFrame:
        return self._load_domain_long(
            self.price_root, symbols=symbols, start=start, end=end, fields=fields
        )

    def load_index_long(
        self,
        *,
        symbols: Sequence[str],
        start: str | None = None,
        end: str | None = None,
        fields: Sequence[str] = ("close",),
    ) -> pd.DataFrame:
        return self._load_domain_long(
            self.index_root, symbols=symbols, start=start, end=end, fields=fields
        )

    def load_wide(
        self,
        *,
        symbols: Sequence[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        fields: Sequence[str] = ("close", "total_mv", "is_st"),
    ) -> dict[str, pd.DataFrame]:
        long = self.load_long(symbols=symbols, start=start, end=end, fields=fields)
        out: dict[str, pd.DataFrame] = {}
        for field in fields:
            if field not in long.columns:
                continue
            out[field] = (
                long.pivot(index="trade_date", columns="symbol", values=field)
                .sort_index()
                .sort_index(axis=1)
            )
        return out

    @lru_cache(maxsize=8)
    def calendar(self, start: str | None = None, end: str | None = None) -> tuple[pd.Timestamp, ...]:
        if not self.calendar_path.exists():
            raise FileNotFoundError(f"交易日历不存在：{self.calendar_path}")
        frame = pd.read_parquet(self.calendar_path, columns=["trade_date"])
        dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna().drop_duplicates()
        if start:
            dates = dates[dates >= pd.Timestamp(start)]
        if end:
            dates = dates[dates <= pd.Timestamp(end)]
        return tuple(pd.Timestamp(value).normalize() for value in dates.sort_values())

    @lru_cache(maxsize=1)
    def industry_table(self) -> pd.DataFrame:
        if not self.industry_path.exists():
            raise FileNotFoundError(f"行业缓存不存在：{self.industry_path}")
        frame = pd.read_csv(self.industry_path, dtype={"code": str})
        frame["code"] = frame["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        frame["industry"] = frame["industry"].fillna("未分类").astype(str)
        frame["industry_source"] = frame.get("source", "sw")
        return frame[["code", "industry", "industry_source"]].drop_duplicates("code")

    @lru_cache(maxsize=1)
    def dividend_records(self) -> pd.DataFrame:
        if not self.dividend_path.exists():
            return pd.DataFrame(
                columns=["code", "ex_date", "payable_date", "cash_per_share"]
            )
        raw = pd.read_parquet(self.dividend_path)
        lot = pd.to_numeric(raw.get("round_lot"), errors="coerce").replace(0, pd.NA).fillna(10.0)
        out = pd.DataFrame(
            {
                "code": raw["symbol"].astype(str).str.zfill(6),
                "ex_date": pd.to_datetime(
                    raw["ex_dividend_date"].astype("Int64").astype(str).str[:8],
                    format="%Y%m%d",
                    errors="coerce",
                ),
                "payable_date": pd.to_datetime(
                    raw["payable_date"].astype("Int64").astype(str).str[:8],
                    format="%Y%m%d",
                    errors="coerce",
                ),
                "cash_per_share": pd.to_numeric(
                    raw["dividend_cash_before_tax"], errors="coerce"
                )
                / lot.astype(float),
            }
        )
        return out.dropna(subset=["code", "ex_date", "cash_per_share"]).sort_values("ex_date")

    @lru_cache(maxsize=1)
    def split_records(self) -> pd.DataFrame:
        if not self.split_path.exists():
            return pd.DataFrame(columns=["code", "ex_date", "factor"])
        raw = pd.read_parquet(self.split_path)
        out = pd.DataFrame(
            {
                "code": raw["symbol"].astype(str).str.zfill(6),
                "ex_date": pd.to_datetime(
                    raw["ex_date"].astype("Int64").astype(str).str[:8],
                    format="%Y%m%d",
                    errors="coerce",
                ),
                "factor": pd.to_numeric(raw["split_factor"], errors="coerce"),
            }
        )
        return out.dropna(subset=["code", "ex_date", "factor"]).sort_values("ex_date")

    def exact_trade_mask(self, long_prices: pd.DataFrame) -> set[tuple[str, pd.Timestamp]]:
        if long_prices.empty:
            return set()
        volume = pd.to_numeric(long_prices.get("volume"), errors="coerce")
        valid = long_prices["close"].gt(0)
        if volume is not None:
            valid &= volume.fillna(0).gt(0)
        return {
            (str(row.symbol), pd.Timestamp(row.trade_date).normalize())
            for row in long_prices.loc[valid, ["symbol", "trade_date"]].itertuples(index=False)
        }


__all__ = ["PriceLoader"]
