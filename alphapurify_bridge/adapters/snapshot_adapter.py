"""Load factor snapshots and correctly aligned forward returns."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Sequence

import duckdb
import numpy as np
import pandas as pd

from factor_snapshot_loader import load_snapshots
from alphapurify_bridge.adapters.cache import FactorDataCache


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT_PATH = ROOT / "data" / "parquet" / "factors" / "snapshots"
DEFAULT_STOCK_DAILY_PATH = ROOT / "data" / "parquet" / "stock_daily"
DEFAULT_CALENDAR_PATH = ROOT / "data" / "parquet" / "trade_calendar" / "calendar.parquet"


def _normalize_date(value: str | date | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise ValueError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _collect_panel_aggregates(
    con: duckdb.DuckDBPyConnection,
    *,
    horizons: Sequence[int],
    primary_horizon: int,
    ic_method: str,
    min_observations: int,
    n_quantiles: int,
    rebalance_freq: str,
    histogram_bins: int,
) -> dict[str, object]:
    """Collect compact result frames from a ``factor_panel`` temp view."""
    if ic_method == "spearman":
        ranks = ", ".join(
            f"CASE WHEN forward_return_{horizon} IS NOT NULL THEN rank() OVER (PARTITION BY trade_date ORDER BY forward_return_{horizon}) END AS return_rank_{horizon}"
            for horizon in horizons
        )
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW ic_input AS SELECT *, rank() OVER (PARTITION BY trade_date ORDER BY factor_value) AS factor_rank, {ranks} FROM factor_panel"
        )
        x_name = "factor_rank"
        y_name = lambda horizon: f"return_rank_{horizon}"
    else:
        con.execute("CREATE OR REPLACE TEMP VIEW ic_input AS SELECT * FROM factor_panel")
        x_name = "factor_value"
        y_name = lambda horizon: f"forward_return_{horizon}"
    ic_expressions = ", ".join(
        f"CASE WHEN count({y_name(horizon)}) >= {max(2, int(min_observations))} THEN corr({x_name}, {y_name(horizon)}) END AS ic_{horizon}"
        for horizon in horizons
    )
    ic_frame = con.execute(
        f"SELECT trade_date, {ic_expressions} FROM ic_input GROUP BY trade_date ORDER BY trade_date"
    ).df()
    period_expression = {
        "M": "date_trunc('month', trade_date)",
        "Q": "date_trunc('quarter', trade_date)",
        "D": "trade_date",
    }[rebalance_freq]
    quantile_frame = con.execute(
        f"""
        WITH period_ends AS (
            SELECT {period_expression} AS period, max(trade_date) AS trade_date
            FROM factor_panel GROUP BY 1
        ), ranked AS (
            SELECT p.trade_date,
                   ntile({int(n_quantiles)}) OVER (PARTITION BY p.trade_date ORDER BY p.factor_value) AS quantile,
                   p.forward_return_{primary_horizon} AS forward_return
            FROM factor_panel p
            INNER JOIN period_ends e USING (trade_date)
            WHERE p.forward_return_{primary_horizon} IS NOT NULL
        )
        SELECT trade_date, quantile, avg(forward_return) AS forward_return
        FROM ranked GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).df()
    factor_return_frame = con.execute(
        f"""
        WITH normalized AS (
            SELECT trade_date,
                   (factor_value - avg(factor_value) OVER (PARTITION BY trade_date))
                   / nullif(stddev_samp(factor_value) OVER (PARTITION BY trade_date), 0) AS exposure,
                   forward_return_{primary_horizon} AS forward_return
            FROM factor_panel
            WHERE forward_return_{primary_horizon} IS NOT NULL
        )
        SELECT trade_date,
               sum(exposure * forward_return) / nullif(sum(exposure * exposure), 0) AS factor_return
        FROM normalized GROUP BY trade_date
        HAVING count(*) >= {max(2, int(min_observations))}
        ORDER BY trade_date
        """
    ).df()
    stats = con.execute(
        f"SELECT count(forward_return_{primary_horizon}), count(distinct trade_date), min(factor_value), max(factor_value) FROM factor_panel WHERE forward_return_{primary_horizon} IS NOT NULL"
    ).fetchone()
    minimum, maximum = stats[2], stats[3]
    histogram: dict[str, object] = {"edges": [], "counts": []}
    if minimum is not None and maximum is not None:
        if float(minimum) == float(maximum):
            histogram = {"edges": [float(minimum), float(maximum)], "counts": [int(stats[0])]}
        else:
            bins = max(5, int(histogram_bins))
            histogram_rows = con.execute(
                f"""
                SELECT least({bins - 1}, floor((factor_value - ?) / (? - ?) * {bins}))::INTEGER AS bin,
                       count(*) AS count
                FROM factor_panel GROUP BY 1 ORDER BY 1
                """,
                [minimum, maximum, minimum],
            ).fetchall()
            counts = [0] * bins
            for bin_number, count in histogram_rows:
                counts[int(bin_number)] = int(count)
            histogram = {
                "edges": np.linspace(float(minimum), float(maximum), bins + 1).tolist(),
                "counts": counts,
            }
    return {
        "ic": ic_frame,
        "quantile": quantile_frame,
        "factor_return": factor_return_frame,
        "histogram": histogram,
        "sample_count": int(stats[0]),
        "cross_section_count": int(stats[1]),
    }


class SnapshotAdapter:
    """Join read-only factor snapshots with N-observation forward close returns.

    Duplicate stock-daily keys are collapsed only when all duplicated close values
    agree. Conflicting duplicates become null returns instead of being selected
    arbitrarily.
    """

    def __init__(
        self,
        snapshot_path: str | Path = DEFAULT_SNAPSHOT_PATH,
        stock_daily_path: str | Path = DEFAULT_STOCK_DAILY_PATH,
        *,
        calendar_path: str | Path = DEFAULT_CALENDAR_PATH,
        cache_enabled: bool = True,
        threads: int = 8,
    ):
        self.snapshot_path = self._resolve(snapshot_path)
        self.stock_daily_path = self._resolve(stock_daily_path)
        self.calendar_path = self._resolve(calendar_path)
        self.cache_enabled = bool(cache_enabled)
        self.threads = max(1, int(threads))
        self._snapshot_cache = FactorDataCache[pd.DataFrame](max_entries=8)
        self._return_cache = FactorDataCache[pd.DataFrame](max_entries=16)
        self._diagnostic_cache = FactorDataCache[dict[str, object]](max_entries=64)
        self.last_profile: dict[str, object] = {}

    @staticmethod
    def _resolve(path: str | Path) -> Path:
        target = Path(path)
        return target.resolve() if target.is_absolute() else (ROOT / target).resolve()

    def available_factors(self) -> list[str]:
        manifest = self.snapshot_path / "manifest.json"
        if not manifest.is_file():
            raise FileNotFoundError(f"Snapshot manifest 不存在：{manifest}")
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        factors = raw.get("factors", [])
        return [str(value) for value in factors] if isinstance(factors, list) else list(factors)

    def _validate_factors(self, factor_names: Sequence[str]) -> tuple[str, ...]:
        names = tuple(dict.fromkeys(str(value).strip() for value in factor_names))
        if not names:
            raise ValueError("至少需要一个因子")
        unknown = sorted(set(names) - set(self.available_factors()))
        if unknown:
            raise ValueError(f"Snapshot 中不存在因子：{', '.join(unknown)}")
        return names

    def _load_snapshot_values(
        self,
        factor_names: Sequence[str],
        start_date: str | None,
        end_date: str | None,
    ) -> pd.DataFrame:
        names = self._validate_factors(factor_names)
        key = (names, start_date, end_date)
        cached = self._snapshot_cache.get(key)
        if cached is None:
            requested = set(names)
            for cached_key, candidate in self._snapshot_cache.items():
                cached_names, cached_start, cached_end = cached_key
                if cached_start == start_date and cached_end == end_date and requested.issubset(cached_names):
                    return candidate.loc[:, ["trade_date", "symbol", *names]].copy(deep=False)
        if cached is None:
            cached = load_snapshots(
                start=start_date,
                end=end_date,
                factors=list(names),
                snapshot_root=self.snapshot_path,
            )
            cached["trade_date"] = pd.to_datetime(cached["trade_date"]).dt.normalize()
            cached["symbol"] = cached["symbol"].astype(str).str.zfill(6)
            if self.cache_enabled:
                self._snapshot_cache.set(key, cached)
        return cached.copy(deep=False)

    def _expanded_end(self, end_date: str | None, horizon: int) -> str | None:
        if end_date is None or not self.calendar_path.is_file():
            return None
        calendar = pd.read_parquet(self.calendar_path, columns=["trade_date"])
        dates = pd.DatetimeIndex(pd.to_datetime(calendar["trade_date"], errors="coerce").dropna().unique()).sort_values()
        target = pd.Timestamp(end_date)
        position = int(dates.searchsorted(target, side="right")) + horizon - 1
        return dates[min(position, len(dates) - 1)].date().isoformat() if len(dates) else end_date

    def load_forward_returns(
        self,
        start_date: str | date | datetime | None,
        end_date: str | date | datetime | None,
        *,
        horizon: int = 1,
    ) -> pd.DataFrame:
        start = _normalize_date(start_date)
        end = _normalize_date(end_date)
        horizon = int(horizon)
        if horizon < 1:
            raise ValueError("horizon 必须为正整数")
        if start and end and start > end:
            raise ValueError("start_date 不能晚于 end_date")
        key = (start, end, horizon)
        cached = self._return_cache.get(key)
        if cached is not None:
            return cached.copy(deep=False)
        files = list(self.stock_daily_path.glob("year=*/*.parquet"))
        if not files:
            raise FileNotFoundError(f"stock_daily Parquet 不存在：{self.stock_daily_path}")
        expanded_end = self._expanded_end(end, horizon)
        source = f"read_parquet('{_sql_path(self.stock_daily_path)}/year=*/*.parquet', hive_partitioning=true, union_by_name=true)"
        predicates: list[str] = ["close IS NOT NULL", "close > 0"]
        params: list[object] = []
        if start:
            predicates.append("trade_date::DATE >= ?::DATE")
            params.append(start)
        if expanded_end:
            predicates.append("trade_date::DATE <= ?::DATE")
            params.append(expanded_end)
        outer: list[str] = ["forward_return IS NOT NULL"]
        if start:
            outer.append("trade_date >= ?::DATE")
            params.append(start)
        if end:
            outer.append("trade_date <= ?::DATE")
            params.append(end)
        query = f"""
            WITH daily AS (
                SELECT
                    trade_date::DATE AS trade_date,
                    lpad(symbol::VARCHAR, 6, '0') AS symbol,
                    CASE WHEN min(close) = max(close) THEN max(close)::DOUBLE ELSE NULL END AS close
                FROM {source}
                WHERE {' AND '.join(predicates)}
                GROUP BY 1, 2
            ), aligned AS (
                SELECT
                    trade_date,
                    symbol,
                    close,
                    lead(close, {horizon}) OVER (PARTITION BY symbol ORDER BY trade_date) / close - 1.0 AS forward_return
                FROM daily
            )
            SELECT trade_date, symbol, close, forward_return
            FROM aligned
            WHERE {' AND '.join(outer)}
            ORDER BY trade_date, symbol
        """
        with duckdb.connect() as con:
            con.execute(f"SET threads TO {self.threads}")
            cached = con.execute(query, params).to_arrow_table().to_pandas(date_as_object=False)
        cached["trade_date"] = pd.to_datetime(cached["trade_date"]).dt.normalize()
        if self.cache_enabled:
            self._return_cache.set(key, cached)
        return cached.copy(deep=False)

    def load_factors(
        self,
        factor_names: Sequence[str],
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        *,
        horizon: int = 1,
        include_close: bool = False,
    ) -> pd.DataFrame:
        start = _normalize_date(start_date)
        end = _normalize_date(end_date)
        names = self._validate_factors(factor_names)
        factors = self._load_snapshot_values(names, start, end)
        returns = self.load_forward_returns(start, end, horizon=horizon)
        merged = factors.merge(returns, on=["trade_date", "symbol"], how="inner", validate="one_to_one")
        columns = ["trade_date", "symbol", *names]
        if include_close:
            columns.append("close")
        columns.append("forward_return")
        return merged.loc[:, columns].sort_values(["trade_date", "symbol"], ignore_index=True)

    def load_factor(
        self,
        factor_name: str,
        start_date: str | date | datetime | None = None,
        end_date: str | date | datetime | None = None,
        *,
        horizon: int = 1,
        include_close: bool = False,
    ) -> pd.DataFrame:
        frame = self.load_factors(
            [factor_name],
            start_date,
            end_date,
            horizon=horizon,
            include_close=include_close,
        )
        return frame.rename(columns={factor_name: "factor_value"})

    def aggregate_factor_diagnostics(
        self,
        factor_name: str,
        start_date: str | date | datetime | None,
        end_date: str | date | datetime | None,
        *,
        horizons: Sequence[int],
        direction: int = 1,
        ic_method: str = "spearman",
        primary_horizon: int = 1,
        n_quantiles: int = 10,
        rebalance_freq: str = "M",
        min_observations: int = 20,
        histogram_bins: int = 30,
    ) -> dict[str, object]:
        """Aggregate diagnostics in DuckDB without materializing the full joined panel."""

        prep_started = time.perf_counter()
        self._validate_factors([factor_name])
        start = _normalize_date(start_date)
        end = _normalize_date(end_date)
        selected_horizons = tuple(dict.fromkeys(int(value) for value in horizons))
        if not selected_horizons or any(value < 1 for value in selected_horizons):
            raise ValueError("horizons 必须是正整数列表")
        primary_horizon = int(primary_horizon)
        if primary_horizon not in selected_horizons:
            raise ValueError("primary_horizon 必须包含在 horizons 中")
        if int(direction) not in {-1, 1}:
            raise ValueError("direction 必须为 1 或 -1")
        frequency = str(rebalance_freq).upper()
        if frequency not in {"M", "Q", "D"}:
            raise ValueError("rebalance_freq 必须为 M、Q 或 D")
        method = str(ic_method).lower()
        if method not in {"pearson", "spearman"}:
            raise ValueError("ic_method 必须为 pearson 或 spearman")
        cache_configuration = (
            selected_horizons,
            int(direction),
            method,
            primary_horizon,
            int(n_quantiles),
            frequency,
            int(min_observations),
            int(histogram_bins),
        )
        cached_aggregate = self._diagnostic_cache.get_factor_data(
            factor_name, start, end, *cache_configuration
        )
        if cached_aggregate is not None:
            self.last_profile = {
                "stages": {
                    "data_load": 0.0,
                    "data_prep": time.perf_counter() - prep_started,
                    "factor_extract": 0.0,
                    "alphapurify": 0.0,
                    "metrics": 0.0,
                    "report": 0.0,
                    "serialize": 0.0,
                },
                "per_factor": {factor_name: {"metrics": 0.0}},
                "cache_hits": [factor_name],
                "computed_factors": [],
            }
            return cached_aggregate
        expanded_end = self._expanded_end(end, max(selected_horizons))
        snapshot_source = f"read_parquet('{_sql_path(self.snapshot_path)}/trade_date=*/factors.parquet', hive_partitioning=true, union_by_name=true)"
        price_source = f"read_parquet('{_sql_path(self.stock_daily_path)}/year=*/*.parquet', hive_partitioning=true, union_by_name=true)"
        price_filters = ["close IS NOT NULL", "close > 0"]
        price_params: list[object] = []
        if start:
            price_filters.append("trade_date::DATE >= ?::DATE")
            price_params.append(start)
        if expanded_end:
            price_filters.append("trade_date::DATE <= ?::DATE")
            price_params.append(expanded_end)
        snapshot_filters = [f'"{factor_name}" IS NOT NULL']
        snapshot_params: list[object] = []
        if start:
            snapshot_filters.append("trade_date::DATE >= ?::DATE")
            snapshot_params.append(start)
        if end:
            snapshot_filters.append("trade_date::DATE <= ?::DATE")
            snapshot_params.append(end)
        leads = ",\n".join(
            f"lead(close, {horizon}) OVER (PARTITION BY symbol ORDER BY trade_date) / close - 1.0 AS forward_return_{horizon}"
            for horizon in selected_horizons
        )
        panel_sql = f"""
            CREATE TEMP TABLE diagnosis_panel AS
            WITH daily AS (
                SELECT trade_date::DATE AS trade_date,
                       lpad(symbol::VARCHAR, 6, '0') AS symbol,
                       CASE WHEN min(close) = max(close) THEN max(close)::DOUBLE ELSE NULL END AS close
                FROM {price_source}
                WHERE {' AND '.join(price_filters)}
                GROUP BY 1, 2
            ), returns AS (
                SELECT trade_date, symbol, close, {leads}
                FROM daily
            ), factors AS (
                SELECT trade_date::DATE AS trade_date,
                       lpad(symbol::VARCHAR, 6, '0') AS symbol,
                       "{factor_name}"::DOUBLE * {int(direction)} AS factor_value
                FROM {snapshot_source}
                WHERE {' AND '.join(snapshot_filters)}
            )
            SELECT factors.trade_date, factors.factor_value,
                   {', '.join(f'returns.forward_return_{horizon}' for horizon in selected_horizons)}
            FROM factors
            INNER JOIN returns USING (trade_date, symbol)
        """
        prep_elapsed = time.perf_counter() - prep_started
        with duckdb.connect() as con:
            con.execute(f"SET threads TO {self.threads}")
            load_started = time.perf_counter()
            con.execute(panel_sql, [*price_params, *snapshot_params])
            load_elapsed = time.perf_counter() - load_started
            metrics_started = time.perf_counter()
            if method == "spearman":
                ranks = ", ".join(
                    f"CASE WHEN forward_return_{horizon} IS NOT NULL THEN rank() OVER (PARTITION BY trade_date ORDER BY forward_return_{horizon}) END AS return_rank_{horizon}"
                    for horizon in selected_horizons
                )
                con.execute(
                    f"CREATE TEMP VIEW ic_input AS SELECT *, rank() OVER (PARTITION BY trade_date ORDER BY factor_value) AS factor_rank, {ranks} FROM diagnosis_panel"
                )
                x_name = "factor_rank"
                y_name = lambda horizon: f"return_rank_{horizon}"
            else:
                con.execute("CREATE TEMP VIEW ic_input AS SELECT * FROM diagnosis_panel")
                x_name = "factor_value"
                y_name = lambda horizon: f"forward_return_{horizon}"
            ic_expressions = ", ".join(
                f"CASE WHEN count({y_name(horizon)}) >= {max(2, int(min_observations))} THEN corr({x_name}, {y_name(horizon)}) END AS ic_{horizon}"
                for horizon in selected_horizons
            )
            ic_frame = con.execute(
                f"SELECT trade_date, {ic_expressions} FROM ic_input GROUP BY trade_date ORDER BY trade_date"
            ).df()
            period_expression = {
                "M": "date_trunc('month', trade_date)",
                "Q": "date_trunc('quarter', trade_date)",
                "D": "trade_date",
            }[frequency]
            quantile_frame = con.execute(
                f"""
                WITH period_ends AS (
                    SELECT {period_expression} AS period, max(trade_date) AS trade_date
                    FROM diagnosis_panel GROUP BY 1
                ), ranked AS (
                    SELECT p.trade_date,
                           ntile({int(n_quantiles)}) OVER (PARTITION BY p.trade_date ORDER BY p.factor_value) AS quantile,
                           p.forward_return_{primary_horizon} AS forward_return
                    FROM diagnosis_panel p
                    INNER JOIN period_ends e USING (trade_date)
                    WHERE p.forward_return_{primary_horizon} IS NOT NULL
                )
                SELECT trade_date, quantile, avg(forward_return) AS forward_return
                FROM ranked GROUP BY 1, 2 ORDER BY 1, 2
                """
            ).df()
            factor_return_frame = con.execute(
                f"""
                WITH normalized AS (
                    SELECT trade_date,
                           (factor_value - avg(factor_value) OVER (PARTITION BY trade_date))
                           / nullif(stddev_samp(factor_value) OVER (PARTITION BY trade_date), 0) AS exposure,
                           forward_return_{primary_horizon} AS forward_return
                    FROM diagnosis_panel
                    WHERE forward_return_{primary_horizon} IS NOT NULL
                )
                SELECT trade_date,
                       sum(exposure * forward_return) / nullif(sum(exposure * exposure), 0) AS factor_return
                FROM normalized GROUP BY trade_date
                HAVING count(*) >= {max(2, int(min_observations))}
                ORDER BY trade_date
                """
            ).df()
            stats = con.execute(
                f"SELECT count(forward_return_{primary_horizon}), count(distinct trade_date), min(factor_value), max(factor_value) FROM diagnosis_panel WHERE forward_return_{primary_horizon} IS NOT NULL"
            ).fetchone()
            minimum, maximum = stats[2], stats[3]
            histogram: dict[str, object] = {"edges": [], "counts": []}
            if minimum is not None and maximum is not None:
                if float(minimum) == float(maximum):
                    histogram = {"edges": [float(minimum), float(maximum)], "counts": [int(stats[0])]}
                else:
                    bins = max(5, int(histogram_bins))
                    histogram_rows = con.execute(
                        f"""
                        SELECT least({bins - 1}, floor((factor_value - ?) / (? - ?) * {bins}))::INTEGER AS bin,
                               count(*) AS count
                        FROM diagnosis_panel
                        GROUP BY 1 ORDER BY 1
                        """,
                        [minimum, maximum, minimum],
                    ).fetchall()
                    counts = [0] * bins
                    for bin_number, count in histogram_rows:
                        counts[int(bin_number)] = int(count)
                    histogram = {
                        "edges": np.linspace(float(minimum), float(maximum), bins + 1).tolist(),
                        "counts": counts,
                    }
            metrics_elapsed = time.perf_counter() - metrics_started
        self.last_profile = {
            "stages": {
                "data_load": load_elapsed,
                "data_prep": prep_elapsed,
                "factor_extract": 0.0,
                "alphapurify": 0.0,
                "metrics": metrics_elapsed,
                "report": 0.0,
                "serialize": 0.0,
            },
            "per_factor": {factor_name: {"metrics": metrics_elapsed}},
        }
        result: dict[str, object] = {
            "ic": ic_frame,
            "quantile": quantile_frame,
            "factor_return": factor_return_frame,
            "histogram": histogram,
            "sample_count": int(stats[0]),
            "cross_section_count": int(stats[1]),
        }
        if self.cache_enabled:
            self._diagnostic_cache.set_factor_data(
                factor_name, start, end, result, *cache_configuration
            )
        return result

    def aggregate_factors_diagnostics(
        self,
        factor_names: Sequence[str],
        start_date: str | date | datetime | None,
        end_date: str | date | datetime | None,
        *,
        horizons: Sequence[int],
        directions: dict[str, int],
        ic_method: str = "spearman",
        primary_horizon: int = 1,
        n_quantiles: int = 10,
        rebalance_freq: str = "M",
        min_observations: int = 20,
        histogram_bins: int = 30,
    ) -> dict[str, dict[str, object]]:
        """Aggregate several factors while scanning prices and snapshots only once."""
        prep_started = time.perf_counter()
        names = self._validate_factors(factor_names)
        requested_names = names
        start = _normalize_date(start_date)
        end = _normalize_date(end_date)
        selected_horizons = tuple(dict.fromkeys(int(value) for value in horizons))
        if not selected_horizons or any(value < 1 for value in selected_horizons):
            raise ValueError("horizons 必须是正整数列表")
        primary_horizon = int(primary_horizon)
        if primary_horizon not in selected_horizons:
            raise ValueError("primary_horizon 必须包含在 horizons 中")
        frequency = str(rebalance_freq).upper()
        if frequency not in {"M", "Q", "D"}:
            raise ValueError("rebalance_freq 必须为 M、Q 或 D")
        method = str(ic_method).lower()
        if method not in {"pearson", "spearman"}:
            raise ValueError("ic_method 必须为 pearson 或 spearman")
        for name in names:
            if int(directions.get(name, 1)) not in {-1, 1}:
                raise ValueError(f"因子 {name} 的 direction 必须为 1 或 -1")
        def cache_configuration(name: str) -> tuple[object, ...]:
            return (
                selected_horizons,
                int(directions.get(name, 1)),
                method,
                primary_horizon,
                int(n_quantiles),
                frequency,
                int(min_observations),
                int(histogram_bins),
            )

        output: dict[str, dict[str, object]] = {}
        cache_hits: list[str] = []
        missing_names: list[str] = []
        for name in requested_names:
            cached = self._diagnostic_cache.get_factor_data(
                name, start, end, *cache_configuration(name)
            )
            if cached is None:
                missing_names.append(name)
            else:
                output[name] = cached
                cache_hits.append(name)
        if not missing_names:
            self.last_profile = {
                "stages": {
                    "data_load": 0.0,
                    "data_prep": time.perf_counter() - prep_started,
                    "factor_extract": 0.0,
                    "alphapurify": 0.0,
                    "metrics": 0.0,
                    "report": 0.0,
                    "serialize": 0.0,
                },
                "per_factor": {name: {"metrics": 0.0} for name in requested_names},
                "cache_hits": cache_hits,
                "computed_factors": [],
            }
            return output
        names = tuple(missing_names)
        expanded_end = self._expanded_end(end, max(selected_horizons))
        snapshot_source = f"read_parquet('{_sql_path(self.snapshot_path)}/trade_date=*/factors.parquet', hive_partitioning=true, union_by_name=true)"
        price_source = f"read_parquet('{_sql_path(self.stock_daily_path)}/year=*/*.parquet', hive_partitioning=true, union_by_name=true)"
        price_filters = ["close IS NOT NULL", "close > 0"]
        price_params: list[object] = []
        if start:
            price_filters.append("trade_date::DATE >= ?::DATE")
            price_params.append(start)
        if expanded_end:
            price_filters.append("trade_date::DATE <= ?::DATE")
            price_params.append(expanded_end)
        snapshot_filters: list[str] = []
        snapshot_params: list[object] = []
        if start:
            snapshot_filters.append("trade_date::DATE >= ?::DATE")
            snapshot_params.append(start)
        if end:
            snapshot_filters.append("trade_date::DATE <= ?::DATE")
            snapshot_params.append(end)
        leads = ",\n".join(
            f"lead(close, {horizon}) OVER (PARTITION BY symbol ORDER BY trade_date) / close - 1.0 AS forward_return_{horizon}"
            for horizon in selected_horizons
        )
        factor_select = ", ".join(
            f'"{name}"::DOUBLE * {int(directions.get(name, 1))} AS factor_{index}'
            for index, name in enumerate(names)
        )
        where_snapshot = "WHERE " + " AND ".join(snapshot_filters) if snapshot_filters else ""
        panel_sql = f"""
            CREATE TEMP TABLE diagnosis_panel AS
            WITH daily AS (
                SELECT trade_date::DATE AS trade_date,
                       lpad(symbol::VARCHAR, 6, '0') AS symbol,
                       CASE WHEN min(close) = max(close) THEN max(close)::DOUBLE ELSE NULL END AS close
                FROM {price_source}
                WHERE {' AND '.join(price_filters)}
                GROUP BY 1, 2
            ), returns AS (
                SELECT trade_date, symbol, {leads} FROM daily
            ), factors AS (
                SELECT trade_date::DATE AS trade_date,
                       lpad(symbol::VARCHAR, 6, '0') AS symbol,
                       {factor_select}
                FROM {snapshot_source} {where_snapshot}
            )
            SELECT factors.trade_date,
                   {', '.join(f'factors.factor_{index}' for index in range(len(names)))},
                   {', '.join(f'returns.forward_return_{horizon}' for horizon in selected_horizons)}
            FROM factors INNER JOIN returns USING (trade_date, symbol)
        """
        prep_elapsed = time.perf_counter() - prep_started
        per_factor_profile: dict[str, dict[str, float]] = {
            name: {"metrics": 0.0} for name in cache_hits
        }
        with duckdb.connect() as con:
            con.execute(f"SET threads TO {self.threads}")
            con.execute("SET preserve_insertion_order TO false")
            load_started = time.perf_counter()
            con.execute(panel_sql, [*price_params, *snapshot_params])
            load_elapsed = time.perf_counter() - load_started
            return_columns = ", ".join(f"forward_return_{horizon}" for horizon in selected_horizons)
            for index, name in enumerate(names):
                con.execute(
                    f"CREATE OR REPLACE TEMP VIEW factor_panel AS SELECT trade_date, factor_{index} AS factor_value, {return_columns} FROM diagnosis_panel WHERE factor_{index} IS NOT NULL"
                )
                metrics_started = time.perf_counter()
                output[name] = _collect_panel_aggregates(
                    con,
                    horizons=selected_horizons,
                    primary_horizon=primary_horizon,
                    ic_method=method,
                    min_observations=min_observations,
                    n_quantiles=n_quantiles,
                    rebalance_freq=frequency,
                    histogram_bins=histogram_bins,
                )
                per_factor_profile[name] = {"metrics": time.perf_counter() - metrics_started}
                if self.cache_enabled:
                    self._diagnostic_cache.set_factor_data(
                        name, start, end, output[name], *cache_configuration(name)
                    )
        self.last_profile = {
            "stages": {
                "data_load": load_elapsed,
                "data_prep": prep_elapsed,
                "factor_extract": 0.0,
                "alphapurify": 0.0,
                "metrics": sum(value["metrics"] for value in per_factor_profile.values()),
                "report": 0.0,
                "serialize": 0.0,
            },
            "per_factor": per_factor_profile,
            "cache_hits": cache_hits,
            "computed_factors": list(names),
        }
        return {name: output[name] for name in requested_names}

    def clear_cache(self) -> None:
        self._snapshot_cache.clear()
        self._return_cache.clear()
        self._diagnostic_cache.clear()


__all__ = ["SnapshotAdapter"]
