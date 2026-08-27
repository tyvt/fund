"""Build the extended, point-in-time factor set used by fusion_v2.

The strategy rebalances monthly, therefore the extended factors are materialised
on the last trading day of each month.  Financial observations become available
only on their real ``pubDate``; no statutory-date or report-period shortcut is
used when StockDB data is available.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PARQUET_ROOT = ROOT / "data" / "parquet"
FACTOR_ROOT = PARQUET_ROOT / "factors"
MANIFEST_PATH = FACTOR_ROOT / "manifest.json"
STOCK_DAILY_GLOB = PARQUET_ROOT / "stock_daily" / "year=*" / "*.parquet"
QFQ_DAILY_GLOB = PARQUET_ROOT / "stock_daily_qfq" / "year=*" / "*.parquet"
INDUSTRY_PATH = ROOT / "cache" / "dividend_lowvol" / "stock_industry_sw_l1.csv"
LEGACY_FINANCIAL_CACHE = ROOT / "cache" / "fusion_v2" / "fundamentals"
FINANCIAL_ROOT = PARQUET_ROOT / "fundamentals" / "fusion_v2"
CONSOLIDATED_FINANCIALS = FINANCIAL_ROOT / "financial_reports.parquet"
MARKET_DB = ROOT / "data" / "market.duckdb"
SUMMARY_PATH = ROOT / "output" / "factors" / "factor_summary.csv"

FACTOR_NAMES = (
    "roe_ttm",
    "fcf_ev",
    "pe_industry_quantile",
    "gross_margin",
    "reversal_5d",
    "reversal_10d",
)

STOCKDB_FIELDS: dict[str, tuple[str, ...]] = {
    "indicator": (
        "code",
        "statDate",
        "pubDate",
        "roe",
        "gross_profit_margin",
    ),
    "cash_flow": (
        "code",
        "statDate",
        "pubDate",
        "net_operate_cash_flow",
        "fix_intan_other_asset_acqui_cash",
        "cash_and_equivalents_at_end",
    ),
    "balance": (
        "code",
        "statDate",
        "pubDate",
        "total_liability",
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _numeric_column(frame: pd.DataFrame, aliases: Sequence[str]) -> pd.Series:
    for name in aliases:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce")
    raise KeyError(f"缺少字段，允许别名：{', '.join(aliases)}")


def compute_fcf_ev(df: pd.DataFrame) -> pd.Series:
    """Return FCF/EV using positive capex cash outflow and RMB-consistent EV."""

    operating_cash = _numeric_column(
        df, ("net_operate_cash_flow", "operating_cash_flow", "cfo")
    )
    capex = _numeric_column(
        df, ("fix_intan_other_asset_acqui_cash", "capital_expenditure", "capex")
    ).abs()
    market_cap = _numeric_column(df, ("total_mv", "market_cap"))
    liabilities = _numeric_column(df, ("total_liability", "total_liabilities"))
    cash = _numeric_column(
        df, ("cash_and_equivalents_at_end", "cash_and_equivalents", "cash")
    )
    enterprise_value = market_cap + liabilities - cash
    result = (operating_cash - capex) / enterprise_value.where(enterprise_value > 0)
    return result.replace([np.inf, -np.inf], np.nan).rename("fcf_ev")


def _safe_minmax(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    minimum = numeric.min(skipna=True)
    maximum = numeric.max(skipna=True)
    if not math.isfinite(float(minimum)) or not math.isfinite(float(maximum)):
        return pd.Series(np.nan, index=values.index, dtype=float)
    if math.isclose(float(maximum), float(minimum), rel_tol=0.0, abs_tol=1e-15):
        return pd.Series(0.5, index=values.index, dtype=float).where(numeric.notna())
    return (numeric - minimum) / (maximum - minimum)


def compute_pe_industry_quantile(
    df: pd.DataFrame,
    industry_col: str = "industry",
    pe_col: str = "pe_ttm",
    window_years: int = 3,
) -> pd.Series:
    """Compute the agreed cheapness score with strict L1/L2/L3 fallbacks.

    L1 compares the current positive PE with all positive monthly observations
    from the same industry in the trailing window.  A smaller PE receives a
    larger score.  L1 additionally requires at least 20 current industry names
    and two years of positive-PE history for the security.  L2 is current-date
    cross-sectional earnings-yield normalisation.  Non-positive/missing PE and
    positive PE without two years of history receive the neutral L3 value 0.5.
    """

    required = {"trade_date", "symbol", industry_col, pe_col}
    missing = required - set(df.columns)
    if missing:
        raise KeyError(f"PE 分位缺少字段：{', '.join(sorted(missing))}")
    work = df.loc[:, list(required)].copy()
    work["trade_date"] = pd.to_datetime(work["trade_date"])
    work[pe_col] = pd.to_numeric(work[pe_col], errors="coerce")
    work = work.sort_values(["trade_date", "symbol"]).reset_index()
    positive = work[pe_col].gt(0) & work[pe_col].notna()
    first_valid = work["trade_date"].where(positive).groupby(work["symbol"]).cummin()
    history_ok = (work["trade_date"] - first_valid).dt.days.ge(730)
    current_counts = positive.groupby([work["trade_date"], work[industry_col]]).transform("sum")

    result = pd.Series(0.5, index=work.index, dtype=float)
    industry_history: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for industry, group in work.loc[positive].groupby(industry_col, sort=False):
        ordered_group = group.sort_values("trade_date")
        industry_history[str(industry)] = (
            ordered_group["trade_date"].to_numpy(dtype="datetime64[ns]"),
            ordered_group[pe_col].to_numpy(dtype=float),
        )

    for day, locations in work.groupby("trade_date", sort=True).groups.items():
        locations = pd.Index(locations)
        valid_l2 = locations[positive.loc[locations] & history_ok.loc[locations]]
        if len(valid_l2):
            earnings_yield = 1.0 / work.loc[valid_l2, pe_col]
            result.loc[valid_l2] = _safe_minmax(earnings_yield).to_numpy()
        cutoff = day - pd.DateOffset(years=int(window_years))
        current = work.loc[locations]
        for industry, industry_locations in current.groupby(industry_col).groups.items():
            industry_locations = pd.Index(industry_locations)
            l1_locations = industry_locations[
                positive.loc[industry_locations]
                & history_ok.loc[industry_locations]
                & current_counts.loc[industry_locations].ge(20)
            ]
            history_arrays = industry_history.get(str(industry))
            if not len(l1_locations) or history_arrays is None:
                continue
            history_dates, history_values = history_arrays
            lower = np.searchsorted(history_dates, np.datetime64(cutoff), side="left")
            upper = np.searchsorted(history_dates, np.datetime64(day), side="right")
            sorted_history = np.sort(history_values[lower:upper])
            if not len(sorted_history):
                continue
            current_values = work.loc[l1_locations, pe_col].to_numpy(dtype=float)
            percentile = np.searchsorted(sorted_history, current_values, side="right") / len(
                sorted_history
            )
            result.loc[l1_locations] = 1.0 - percentile
    restored = pd.Series(result.to_numpy(), index=work["index"], name="pe_industry_quantile")
    return restored.reindex(df.index).clip(0.0, 1.0)


def compute_reversal(df: pd.DataFrame, window: int = 5) -> pd.Series:
    """Return negative N-session cumulative return, grouped by symbol."""

    if int(window) <= 0:
        raise ValueError("反转窗口必须为正整数")
    if "close" not in df or "symbol" not in df:
        raise KeyError("反转因子需要 close 和 symbol")
    work = df.copy()
    order_columns = ["symbol"] + (["trade_date"] if "trade_date" in work else [])
    ordered = work.sort_values(order_columns)
    close = pd.to_numeric(ordered["close"], errors="coerce")
    previous = close.groupby(ordered["symbol"]).shift(int(window))
    reversal = -(close / previous - 1.0)
    return pd.Series(reversal.to_numpy(), index=ordered.index).reindex(df.index).rename(
        f"reversal_{int(window)}d"
    )


def _stat_periods(start: date, end: date, lookback_years: int = 2) -> list[str]:
    periods: list[str] = []
    for year in range(start.year - int(lookback_years), end.year + 1):
        periods.extend(f"{year}q{quarter}" for quarter in range(1, 5))
    return periods


def _normalise_financial_frame(frame: pd.DataFrame, source: str) -> pd.DataFrame:
    out = frame.copy()
    if out.empty:
        return out
    out.columns = [str(column) for column in out.columns]
    out["symbol"] = (
        out["code"].astype(str).str.split(".", regex=False).str[0].str.extract(r"(\d{6})", expand=False)
    )
    out["stat_date"] = pd.to_datetime(out.get("statDate"), errors="coerce")
    out["pub_date"] = pd.to_datetime(out.get("pubDate"), errors="coerce")
    out["source"] = source
    return out.dropna(subset=["symbol", "stat_date", "pub_date"])


def _stockdb_table(table_name: str, period: str, retries: int = 5) -> pd.DataFrame:
    sdk_path = ROOT.parent / "stockdb" / "pybao"
    if not sdk_path.is_dir():
        raise FileNotFoundError(f"StockDB SDK 不存在：{sdk_path}")
    if str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))
    import stock_sdk as sdk  # type: ignore

    sdk.set_init(df=False)
    table = getattr(sdk, table_name)
    query_fields = [getattr(table, field) for field in STOCKDB_FIELDS[table_name]]
    query_object = sdk.query(*query_fields)
    last_error: Exception | None = None
    for attempt in range(max(1, int(retries))):
        try:
            result = sdk.get_fundamentals(query_object, statDate=period)
            if isinstance(result, list):
                return _normalise_financial_frame(pd.DataFrame(result), "stockdb")
            if isinstance(result, dict) and not result.get("error"):
                return _normalise_financial_frame(pd.DataFrame(result), "stockdb")
            message = str(result)
            raise RuntimeError(message)
        except Exception as exc:  # the online endpoint is intermittently busy
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"StockDB {table_name} {period} 查询失败：{last_error}")


def _cache_path(table_name: str, period: str) -> Path:
    return FINANCIAL_ROOT / "stockdb" / table_name / f"{period}.parquet"


def _legacy_cache_path(table_name: str, period: str) -> Path:
    return LEGACY_FINANCIAL_CACHE / "stockdb" / table_name / f"{period}.parquet"


def _atomic_parquet(frame: pd.DataFrame, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    frame.to_parquet(temporary, index=False)
    os.replace(temporary, target)


def _duckdb_table(table_name: str) -> str:
    if table_name not in STOCKDB_FIELDS:
        raise ValueError(f"未知财务表：{table_name}")
    return f"fusion_financial_{table_name}"


def _ensure_financial_tables() -> None:
    MARKET_DB.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(MARKET_DB)) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS fusion_financial_indicator (
                symbol VARCHAR NOT NULL,
                stat_date DATE NOT NULL,
                pub_date DATE NOT NULL,
                roe DOUBLE,
                gross_profit_margin DOUBLE,
                source VARCHAR NOT NULL,
                synced_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fusion_financial_cash_flow (
                symbol VARCHAR NOT NULL,
                stat_date DATE NOT NULL,
                pub_date DATE NOT NULL,
                net_operate_cash_flow DOUBLE,
                fix_intan_other_asset_acqui_cash DOUBLE,
                cash_and_equivalents_at_end DOUBLE,
                source VARCHAR NOT NULL,
                synced_at TIMESTAMP NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fusion_financial_balance (
                symbol VARCHAR NOT NULL,
                stat_date DATE NOT NULL,
                pub_date DATE NOT NULL,
                total_liability DOUBLE,
                source VARCHAR NOT NULL,
                synced_at TIMESTAMP NOT NULL
            );
            """
        )


def _period_date(period: str) -> date:
    year = int(period[:4])
    quarter = int(period[-1])
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    return date(year, *month_day)


def _duckdb_period_cached(table_name: str, period: str) -> bool:
    target_date = _period_date(period)
    with duckdb.connect(str(MARKET_DB), read_only=True) as con:
        count = con.execute(
            f"SELECT count(*) FROM {_duckdb_table(table_name)} WHERE stat_date = ?",
            [target_date],
        ).fetchone()[0]
    return int(count) > 0


def _write_financial_partition(table_name: str, period: str, frame: pd.DataFrame) -> None:
    if frame.empty:
        return
    value_columns = [
        field
        for field in STOCKDB_FIELDS[table_name]
        if field not in {"code", "statDate", "pubDate"}
    ]
    columns = ["symbol", "stat_date", "pub_date", *value_columns, "source"]
    stored = frame.reindex(columns=columns).copy()
    stored["synced_at"] = pd.Timestamp.now(tz="UTC").tz_localize(None)
    target_date = _period_date(period)
    with duckdb.connect(str(MARKET_DB)) as con:
        con.register("incoming_financials", stored)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                f"DELETE FROM {_duckdb_table(table_name)} WHERE stat_date = ?",
                [target_date],
            )
            con.execute(
                f"INSERT INTO {_duckdb_table(table_name)} SELECT * FROM incoming_financials"
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def _import_legacy_financial_cache() -> None:
    """Migrate already downloaded API partitions into the preferred Parquet lake."""

    for table_name in STOCKDB_FIELDS:
        directory = LEGACY_FINANCIAL_CACHE / "stockdb" / table_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.parquet")):
            period = path.stem.lower()
            target = _cache_path(table_name, period)
            if not target.is_file():
                _atomic_parquet(pd.read_parquet(path), target)


def _read_duckdb_period(table_name: str, period: str) -> pd.DataFrame:
    """Use DuckDB only as the fallback when a Parquet partition is unavailable."""

    if not MARKET_DB.is_file():
        return pd.DataFrame()
    target_date = _period_date(period)
    try:
        with duckdb.connect(str(MARKET_DB), read_only=True) as con:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            }
            table = _duckdb_table(table_name)
            if table not in tables:
                return pd.DataFrame()
            return con.execute(
                f"SELECT * EXCLUDE (synced_at) FROM {table} WHERE stat_date = ?",
                [target_date],
            ).fetchdf()
    except (duckdb.Error, OSError):
        return pd.DataFrame()


def _read_financial_tables(start: date, end: date) -> dict[str, pd.DataFrame]:
    lower = date(start.year - 2, 1, 1)
    upper = date(end.year, 12, 31)
    with duckdb.connect(str(MARKET_DB), read_only=True) as con:
        return {
            table_name: con.execute(
                f"""
                SELECT * EXCLUDE (synced_at)
                FROM {_duckdb_table(table_name)}
                WHERE stat_date BETWEEN ? AND ?
                ORDER BY symbol, stat_date, pub_date
                """,
                [lower, upper],
            ).fetchdf()
            for table_name in STOCKDB_FIELDS
        }


def sync_stockdb_fundamentals(
    start: date,
    end: date,
    *,
    refresh: bool = False,
    retries: int = 5,
) -> tuple[dict[str, pd.DataFrame], list[dict[str, str]]]:
    _import_legacy_financial_cache()
    frames: dict[str, list[pd.DataFrame]] = {name: [] for name in STOCKDB_FIELDS}
    errors: list[dict[str, str]] = []
    periods = _stat_periods(start, end)
    for table_name in STOCKDB_FIELDS:
        for number, period in enumerate(periods, 1):
            target = _cache_path(table_name, period)
            frame = pd.DataFrame()
            if target.is_file() and not refresh:
                frame = pd.read_parquet(target)
            elif not refresh:
                frame = _read_duckdb_period(table_name, period)
                if not frame.empty:
                    _atomic_parquet(frame, target)
            if frame.empty or refresh:
                print(
                    f"StockDB {table_name} {period} [{number}/{len(periods)}]",
                    flush=True,
                )
                try:
                    frame = _stockdb_table(table_name, period, retries=retries)
                    try:
                        _atomic_parquet(frame, target)
                    except (OSError, ValueError, ImportError):
                        # Only fall back to DuckDB when a local Parquet partition
                        # genuinely cannot be formed.
                        _ensure_financial_tables()
                        _write_financial_partition(table_name, period, frame)
                except Exception as exc:
                    errors.append(
                        {"table": table_name, "period": period, "error": f"{type(exc).__name__}: {exc}"}
                    )
                    continue
            if not frame.empty:
                frames[table_name].append(frame)
    combined = {
        name: pd.concat(items, ignore_index=True)
        .sort_values(["symbol", "stat_date", "pub_date"])
        .drop_duplicates(["symbol", "stat_date"], keep="last")
        if items
        else pd.DataFrame()
        for name, items in frames.items()
    }
    return combined, errors


def _period_number(stat_date: pd.Series) -> pd.Series:
    return pd.to_datetime(stat_date).dt.quarter.astype("Int64")


def _ttm_from_ytd(frame: pd.DataFrame, value_col: str) -> pd.Series:
    """Convert cumulative quarterly cash-flow values into TTM values."""

    work = frame[["symbol", "stat_date", value_col]].copy()
    work["year"] = work["stat_date"].dt.year
    work["quarter"] = _period_number(work["stat_date"])
    lookup = work.set_index(["symbol", "year", "quarter"])[value_col]

    def value(row: pd.Series) -> float:
        current = row[value_col]
        if pd.isna(current):
            return np.nan
        quarter = int(row["quarter"])
        if quarter == 4:
            return float(current)
        key_annual = (row["symbol"], int(row["year"]) - 1, 4)
        key_prior = (row["symbol"], int(row["year"]) - 1, quarter)
        annual = lookup.get(key_annual, np.nan)
        prior = lookup.get(key_prior, np.nan)
        if pd.isna(annual) or pd.isna(prior):
            return np.nan
        return float(current) + float(annual) - float(prior)

    return work.apply(value, axis=1)


def consolidate_financials(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    indicator = tables.get("indicator", pd.DataFrame()).copy()
    cash = tables.get("cash_flow", pd.DataFrame()).copy()
    balance = tables.get("balance", pd.DataFrame()).copy()
    if indicator.empty:
        raise RuntimeError("StockDB indicator 无有效数据，无法构建 ROE/毛利率")

    for frame in (indicator, cash, balance):
        if not frame.empty:
            for column in frame.columns:
                if column not in {"code", "symbol", "statDate", "pubDate", "stat_date", "pub_date", "source"}:
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if not cash.empty:
        cash["fcf_ytd"] = (
            cash["net_operate_cash_flow"]
            - cash["fix_intan_other_asset_acqui_cash"].abs()
        )
        cash = cash.sort_values(["symbol", "stat_date"])
        cash["fcf_ttm"] = _ttm_from_ytd(cash, "fcf_ytd")

    base_columns = [
        "symbol",
        "stat_date",
        "pub_date",
        "roe",
        "gross_profit_margin",
        "source",
    ]
    result = indicator.loc[:, [column for column in base_columns if column in indicator]].copy()
    if not cash.empty:
        result = result.merge(
            cash[
                [
                    "symbol",
                    "stat_date",
                    "pub_date",
                    "net_operate_cash_flow",
                    "fix_intan_other_asset_acqui_cash",
                    "cash_and_equivalents_at_end",
                    "fcf_ttm",
                ]
            ].rename(columns={"pub_date": "cash_pub_date"}),
            on=["symbol", "stat_date"],
            how="outer",
        )
    if not balance.empty:
        result = result.merge(
            balance[["symbol", "stat_date", "pub_date", "total_liability"]].rename(
                columns={"pub_date": "balance_pub_date"}
            ),
            on=["symbol", "stat_date"],
            how="outer",
        )
    date_columns = [column for column in ("pub_date", "cash_pub_date", "balance_pub_date") if column in result]
    result["available_date"] = result[date_columns].max(axis=1)
    result["source"] = "stockdb"
    result = result.sort_values(["symbol", "stat_date", "available_date"]).drop_duplicates(
        ["symbol", "stat_date"], keep="last"
    )
    return result.reset_index(drop=True)


def _industry_frame() -> pd.DataFrame:
    if not INDUSTRY_PATH.is_file():
        raise FileNotFoundError(f"行业映射不存在：{INDUSTRY_PATH}")
    industry = pd.read_csv(INDUSTRY_PATH, dtype={"code": str})
    industry["symbol"] = industry["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    return industry[["symbol", "industry"]].drop_duplicates("symbol", keep="last")


def _load_monthly_market(start: date, end: date) -> pd.DataFrame:
    lookback = date(start.year - 3, start.month, 1)
    query = f"""
    WITH raw AS (
        SELECT try_cast(trade_date AS DATE) AS trade_date, symbol,
               max(pe_ttm) AS pe_ttm, max(total_mv) AS total_mv
        FROM read_parquet('{_sql_path(STOCK_DAILY_GLOB)}', hive_partitioning=true, union_by_name=true)
        WHERE try_cast(trade_date AS DATE) BETWEEN DATE '{lookback}' AND DATE '{end}'
        GROUP BY trade_date, symbol
    ), ranked AS (
        SELECT *, row_number() OVER (
            PARTITION BY symbol, year(trade_date), month(trade_date)
            ORDER BY trade_date DESC
        ) AS rn
        FROM raw
    )
    SELECT trade_date, symbol, pe_ttm, total_mv
    FROM ranked WHERE rn = 1
    """
    with duckdb.connect() as con:
        market = con.execute(query).fetchdf()
    market["trade_date"] = pd.to_datetime(market["trade_date"])
    return market.merge(_industry_frame(), on="symbol", how="left").assign(
        industry=lambda frame: frame["industry"].fillna("未分类")
    )


def _load_monthly_reversals(start: date, end: date) -> pd.DataFrame:
    query = f"""
    WITH raw AS (
        SELECT try_cast(trade_date AS DATE) AS trade_date, symbol, max(close) AS close
        FROM read_parquet('{_sql_path(QFQ_DAILY_GLOB)}', hive_partitioning=true, union_by_name=true)
        WHERE try_cast(trade_date AS DATE) <= DATE '{end}'
        GROUP BY trade_date, symbol
    ), lagged AS (
        SELECT *,
            lag(close, 5) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_5d,
            lag(close, 10) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_10d,
            row_number() OVER (
                PARTITION BY symbol, year(trade_date), month(trade_date)
                ORDER BY trade_date DESC
            ) AS rn
        FROM raw
    )
    SELECT trade_date, symbol,
           -(close / nullif(close_5d, 0) - 1.0) AS reversal_5d,
           -(close / nullif(close_10d, 0) - 1.0) AS reversal_10d
    FROM lagged
    WHERE rn = 1 AND trade_date BETWEEN DATE '{start}' AND DATE '{end}'
    """
    with duckdb.connect() as con:
        frame = con.execute(query).fetchdf()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    return frame


def _asof_financials(market: pd.DataFrame, financials: pd.DataFrame) -> pd.DataFrame:
    left = market.sort_values(["trade_date", "symbol"]).copy()
    left["trade_date"] = pd.to_datetime(left["trade_date"]).astype("datetime64[ns]")
    right = financials.dropna(subset=["available_date"]).copy()
    right["available_date"] = pd.to_datetime(right["available_date"]).astype(
        "datetime64[ns]"
    )
    right = right.sort_values(["available_date", "symbol"])
    return pd.merge_asof(
        left,
        right,
        left_on="trade_date",
        right_on="available_date",
        by="symbol",
        direction="backward",
        allow_exact_matches=True,
    )


def _zscore(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    std = numeric.std(ddof=0)
    if pd.isna(std) or float(std) <= 1e-12:
        return pd.Series(0.0, index=values.index).where(numeric.notna())
    return (numeric - numeric.mean()) / std


def build_monthly_factor_panel(start: date, end: date, financials: pd.DataFrame) -> pd.DataFrame:
    market_with_lookback = _load_monthly_market(start, end)
    pe_score = compute_pe_industry_quantile(market_with_lookback)
    market_with_lookback["pe_industry_quantile"] = pe_score
    market = market_with_lookback.loc[
        market_with_lookback["trade_date"].between(pd.Timestamp(start), pd.Timestamp(end))
    ].copy()
    panel = _asof_financials(market, financials)
    panel["roe_ttm"] = panel.groupby(["trade_date", "industry"])["roe"].transform(_zscore)
    panel["gross_margin"] = panel.groupby(["trade_date", "industry"])[
        "gross_profit_margin"
    ].transform(_zscore)
    enterprise_value = (
        panel["total_mv"]
        + pd.to_numeric(panel.get("total_liability"), errors="coerce")
        - pd.to_numeric(panel.get("cash_and_equivalents_at_end"), errors="coerce")
    )
    raw_fcf_ev = pd.to_numeric(panel.get("fcf_ttm"), errors="coerce") / enterprise_value.where(
        enterprise_value > 0
    )
    panel["fcf_ev"] = raw_fcf_ev.groupby(panel["trade_date"]).transform(_zscore)
    reversals = _load_monthly_reversals(start, end)
    return panel.merge(reversals, on=["trade_date", "symbol"], how="left")


def _write_factor(frame: pd.DataFrame, factor: str) -> dict[str, Any]:
    target = FACTOR_ROOT / factor
    staging = FACTOR_ROOT / f".{factor}.tmp-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    data = frame[["trade_date", "symbol", factor]].rename(columns={factor: "value"}).copy()
    data["year"] = data["trade_date"].dt.year
    with duckdb.connect() as con:
        con.register("factor_data", data)
        con.execute(
            f"COPY factor_data TO '{_sql_path(staging)}' "
            "(FORMAT PARQUET, PARTITION_BY(year), COMPRESSION ZSTD, OVERWRITE_OR_IGNORE true)"
        )
    for year_dir in staging.glob("year=*"):
        parquet_files = list(year_dir.glob("*.parquet"))
        if parquet_files:
            parquet_files[0].replace(year_dir / "factors.parquet")
            for extra in parquet_files[1:]:
                extra.unlink()
    backup = FACTOR_ROOT / f".{factor}.old-{uuid.uuid4().hex}"
    if target.exists():
        os.replace(target, backup)
    os.replace(staging, target)
    if backup.exists():
        shutil.rmtree(backup)
    valid = data["value"].notna()
    return {
        "factor": factor,
        "rows": int(len(data)),
        "valid_rows": int(valid.sum()),
        "coverage": float(valid.mean()) if len(data) else 0.0,
        "mean": float(data.loc[valid, "value"].mean()) if valid.any() else None,
        "std": float(data.loc[valid, "value"].std(ddof=0)) if valid.any() else None,
        "min_date": data["trade_date"].min().date().isoformat() if len(data) else None,
        "max_date": data["trade_date"].max().date().isoformat() if len(data) else None,
    }


def _factor_metadata() -> dict[str, dict[str, Any]]:
    common = {"version": "v2", "schedule": "monthly", "source": "fusion_v2"}
    return {
        "roe_ttm": {
            **common,
            "name": "roe_ttm",
            "display_name": "ROE TTM（行业 z-score）",
            "category": "quality",
            "description": "StockDB 最新已公告季度 ROE，申万一级行业内 z-score",
        },
        "fcf_ev": {
            **common,
            "name": "fcf_ev",
            "display_name": "FCF/EV（截面 z-score）",
            "category": "cashflow",
            "description": "TTM(经营现金流-资本支出)/(总市值+总负债-现金)，全截面 z-score",
        },
        "pe_industry_quantile": {
            **common,
            "name": "pe_industry_quantile",
            "display_name": "PE 行业内三年便宜度",
            "category": "valuation",
            "description": "1-行业三年正PE百分位；L2盈利收益率截面归一化；非正/缺失PE为0.5",
        },
        "gross_margin": {
            **common,
            "name": "gross_margin",
            "display_name": "毛利率（行业 z-score）",
            "category": "quality",
            "description": "StockDB 最新已公告毛利率，申万一级行业内 z-score",
        },
        "reversal_5d": {
            **common,
            "name": "reversal_5d",
            "display_name": "5日反转",
            "category": "price",
            "description": "前复权收盘价过去5个交易区间累计收益取负",
        },
        "reversal_10d": {
            **common,
            "name": "reversal_10d",
            "display_name": "10日反转",
            "category": "price",
            "description": "前复权收盘价过去10个交易区间累计收益取负",
        },
    }


def _update_manifest(summaries: list[dict[str, Any]]) -> None:
    if MANIFEST_PATH.is_file():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    else:
        manifest = {"version": "1.0", "factors": {}}
    factors = manifest.setdefault("factors", {})
    summary_map = {row["factor"]: row for row in summaries}
    for name, metadata in _factor_metadata().items():
        row = summary_map[name]
        factors[name] = {
            **metadata,
            "depends_on": [
                "stock_daily",
                "stock_daily_qfq",
                "StockDB get_fundamentals",
                "cache/dividend_lowvol/stock_industry_sw_l1.csv",
            ],
            "last_computed": _utc_now(),
            "row_count": row["rows"],
            "min_date": row["min_date"],
            "max_date": row["max_date"],
        }
    manifest["updated_at"] = _utc_now()
    temp = MANIFEST_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, MANIFEST_PATH)


def build(
    start: date,
    end: date,
    *,
    refresh_fundamentals: bool = False,
    retries: int = 5,
    skip_sync: bool = False,
) -> tuple[pd.DataFrame, list[dict[str, str]]]:
    if start > end:
        raise ValueError("start 不能晚于 end")
    if skip_sync:
        if not CONSOLIDATED_FINANCIALS.is_file():
            raise FileNotFoundError(f"找不到本地财务 Parquet：{CONSOLIDATED_FINANCIALS}")
        financials = pd.read_parquet(CONSOLIDATED_FINANCIALS)
        sync_errors: list[dict[str, str]] = []
    else:
        tables, sync_errors = sync_stockdb_fundamentals(
            start, end, refresh=refresh_fundamentals, retries=retries
        )
        financials = consolidate_financials(tables)
        _atomic_parquet(financials, CONSOLIDATED_FINANCIALS)
    # Factor construction deliberately reloads the materialised local Parquet;
    # no API-return DataFrame is consumed by the research stage.
    financials = pd.read_parquet(CONSOLIDATED_FINANCIALS).sort_values(
        ["symbol", "available_date"]
    )
    panel = build_monthly_factor_panel(start, end, financials)
    summaries = [_write_factor(panel, factor) for factor in FACTOR_NAMES]
    _update_manifest(summaries)
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(summaries)
    summary.to_csv(SUMMARY_PATH, index=False, encoding="utf-8-sig")
    sync_report = SUMMARY_PATH.with_name("financial_sync_report.json")
    sync_report.write_text(
        json.dumps(
            {
                "generated_at": _utc_now(),
                "primary_source": "StockDB get_fundamentals",
                "fallback_source": "AKShare stock_financial_abstract",
                "local_source_of_truth": "data/parquet/fundamentals/fusion_v2",
                "fallback_store": "data/market.duckdb（仅无法形成Parquet时）",
                "fallback_note": "AKShare摘要不含严格FCF/EV所需全部绝对额，仅允许补ROE/毛利率；本次未用近似FCF。",
                "errors": sync_errors,
                "financial_rows": int(len(financials)),
                "factor_rows": int(len(panel)),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary, sync_errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 fusion_v2 扩展因子")
    parser.add_argument("--start", type=_parse_date, default=date(2015, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=date(2024, 12, 31))
    parser.add_argument("--refresh-fundamentals", action="store_true")
    parser.add_argument("--skip-sync", action="store_true", help="复用已合并财务缓存")
    parser.add_argument("--retries", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary, errors = build(
        args.start,
        args.end,
        refresh_fundamentals=args.refresh_fundamentals,
        retries=args.retries,
        skip_sync=args.skip_sync,
    )
    print(summary.to_string(index=False))
    if errors:
        print(f"财务同步存在 {len(errors)} 个失败分区；详见 output/factors/financial_sync_report.json")
    print(f"因子摘要：{SUMMARY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
