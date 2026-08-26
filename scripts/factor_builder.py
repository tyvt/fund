"""Build the first factor-mart datasets from the local Parquet data lake.

The builder never changes source datasets.  Every output part contains exactly
``trade_date``, ``symbol`` and ``value``; ``year`` is supplied by the Hive
partition path when the files are queried.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import duckdb


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alphapurify_bridge.utils import load_industry_mapping


PARQUET_ROOT = ROOT / "data" / "parquet"
FACTOR_ROOT = PARQUET_ROOT / "factors"
MANIFEST_PATH = FACTOR_ROOT / "manifest.json"
STOCK_CODES_PATH = PARQUET_ROOT / "stock_meta" / "stock_codes.json"
STOCK_DAILY_GLOB = PARQUET_ROOT / "stock_daily" / "year=*" / "*.parquet"
INDEX_DAILY_GLOB = PARQUET_ROOT / "index_daily" / "year=*" / "*.parquet"
DIVIDEND_PATH = PARQUET_ROOT / "stock_dividend" / "dividend_events.parquet"
RISK_CSV_PATH = ROOT / "cache" / "dividend_lowvol" / "risk_hist_merged.csv"
INDUSTRY_CSV_PATH = ROOT / "cache" / "dividend_lowvol" / "stock_industry_sw_l1.csv"

BATCH_SIZE = 500
ROE_VOLATILITY_YEARS = 8
FACTOR_NAMES = (
    "dividend_yield",
    "volatility_60d",
    "beta_300",
    "roe",
    "debt_ratio",
    "roe_volatility",
)


def _default_factor_metadata() -> dict[str, dict[str, object]]:
    return {
        "dividend_yield": {
            "name": "dividend_yield",
            "display_name": "股息率 TTM",
            "category": "scoring",
            "description": "过去365天每股现金分红合计 / 当日收盘价（小数）",
            "version": "v1",
            "depends_on": ["stock_daily", "stock_dividend"],
            "schedule": "daily",
        },
        "volatility_60d": {
            "name": "volatility_60d",
            "display_name": "60日年化波动率",
            "category": "risk",
            "description": "最近60个有效交易日对数收益率的样本标准差乘以sqrt(252)",
            "version": "v1",
            "depends_on": ["stock_daily"],
            "schedule": "daily",
        },
        "beta_300": {
            "name": "beta_300",
            "display_name": "沪深300 Beta",
            "category": "risk",
            "description": "最近252个共同交易日个股与沪深300对数收益率的协方差 / 沪深300收益率方差",
            "version": "v1",
            "depends_on": ["stock_daily", "index_daily"],
            "schedule": "daily",
        },
        "roe": {
            "name": "roe",
            "display_name": "ROE",
            "category": "quality",
            "description": "最近可用年报的 roe_pct 减同期申万一级行业均值；年报自次年4月30日起可用",
            "version": "v1",
            "depends_on": [
                "cache/dividend_lowvol/risk_hist_merged.csv",
                "cache/dividend_lowvol/stock_industry_sw_l1.csv",
            ],
            "schedule": "daily",
        },
        "debt_ratio": {
            "name": "debt_ratio",
            "display_name": "资产负债率",
            "category": "risk",
            "description": "最近可用年报的 debt_ratio_pct 减同期申万一级行业均值；年报自次年4月30日起可用",
            "version": "v1",
            "depends_on": [
                "cache/dividend_lowvol/risk_hist_merged.csv",
                "cache/dividend_lowvol/stock_industry_sw_l1.csv",
            ],
            "schedule": "daily",
        },
        "roe_volatility": {
            "name": "roe_volatility",
            "display_name": "ROE 波动率",
            "category": "quality",
            "description": "最近8个年度ROE的样本标准差（ddof=1）",
            "version": "v1",
            "depends_on": ["cache/dividend_lowvol/risk_hist_merged.csv"],
            "schedule": "daily",
        },
    }


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期必须为 YYYY-MM-DD：{value}") from exc


def _chunks(items: Sequence[str], size: int) -> Iterable[tuple[int, list[str]]]:
    for offset in range(0, len(items), size):
        yield offset // size, list(items[offset : offset + size])


def _years_between(start: date, end: date) -> list[int]:
    return list(range(start.year, end.year + 1))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_manifest() -> dict[str, object]:
    if MANIFEST_PATH.exists():
        with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    else:
        manifest = {"version": "1.0", "updated_at": _utc_now(), "factors": {}}

    factors = manifest.setdefault("factors", {})
    assert isinstance(factors, dict)
    for name, defaults in _default_factor_metadata().items():
        current = factors.setdefault(name, {})
        assert isinstance(current, dict)
        # Definitions are code-owned; only computed statistics are preserved.
        for key, value in defaults.items():
            current[key] = value
        current.setdefault("last_computed", None)
        current.setdefault("row_count", 0)
        current.setdefault("min_date", None)
        current.setdefault("max_date", None)
    return manifest


def save_manifest(manifest: dict[str, object]) -> None:
    FACTOR_ROOT.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = _utc_now()
    temp_path = MANIFEST_PATH.with_suffix(".json.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(temp_path, MANIFEST_PATH)


def load_stock_codes(limit: int | None = None) -> list[str]:
    with STOCK_CODES_PATH.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw_codes = payload.get("codes") if isinstance(payload, dict) else payload
    if not isinstance(raw_codes, list):
        raise ValueError(f"股票列表格式无效：{STOCK_CODES_PATH}")
    codes = sorted({str(code).zfill(6) for code in raw_codes})
    return codes[:limit] if limit is not None else codes


def _required_paths(factor_name: str) -> list[Path]:
    common = [STOCK_CODES_PATH, PARQUET_ROOT / "stock_daily"]
    extras = {
        "dividend_yield": [DIVIDEND_PATH],
        "volatility_60d": [],
        "beta_300": [PARQUET_ROOT / "index_daily"],
        "roe": [RISK_CSV_PATH, INDUSTRY_CSV_PATH],
        "debt_ratio": [RISK_CSV_PATH, INDUSTRY_CSV_PATH],
        "roe_volatility": [RISK_CSV_PATH],
    }
    return common + extras[factor_name]


def _preflight(factors: Sequence[str]) -> None:
    missing = sorted({path for name in factors for path in _required_paths(name) if not path.exists()})
    if missing:
        lines = "\n".join(f"  - {path}" for path in missing)
        raise FileNotFoundError(f"因子输入数据缺失：\n{lines}")


def _daily_cte(symbols: Sequence[str], end: date, columns: str = "close") -> str:
    symbol_list = ", ".join(_sql_string(symbol) for symbol in symbols)
    selected = f", {columns}" if columns else ""
    aggregates = ", ".join(f"max({column.strip()}) AS {column.strip()}" for column in columns.split(",") if column.strip())
    aggregate_select = f", {aggregates}" if aggregates else ""
    return f"""
daily_raw AS (
    SELECT try_cast(trade_date AS DATE) AS trade_date, symbol{selected}
    FROM read_parquet('{_sql_path(STOCK_DAILY_GLOB)}', hive_partitioning=true)
    WHERE symbol IN ({symbol_list})
      AND try_cast(trade_date AS DATE) <= DATE '{end.isoformat()}'
),
daily AS (
    -- Older source partitions contain exact duplicate rows in overlapping part files.
    SELECT trade_date, symbol{aggregate_select}
    FROM daily_raw
    WHERE trade_date IS NOT NULL
    GROUP BY trade_date, symbol
)
"""


def _create_dividend_yield(
    con: duckdb.DuckDBPyConnection, symbols: Sequence[str], start: date, end: date
) -> None:
    sql = f"""
CREATE OR REPLACE TEMP TABLE factor_result AS
WITH
{_daily_cte(symbols, end, 'close')},
dividend_events AS (
    SELECT
        symbol,
        try_strptime(cast(ex_dividend_date AS VARCHAR), '%Y%m%d')::DATE AS ex_date,
        dividend_cash_before_tax / coalesce(nullif(round_lot, 0), 10.0) AS cash_per_share
    FROM read_parquet('{_sql_path(DIVIDEND_PATH)}')
    WHERE dividend_cash_before_tax > 0
),
calculated AS (
    SELECT
        d.trade_date,
        d.symbol,
        CASE
            WHEN d.close > 0 AND count(e.cash_per_share) > 0
            THEN sum(e.cash_per_share) / d.close
            ELSE NULL
        END::DOUBLE AS value
    FROM daily d
    LEFT JOIN dividend_events e
      ON e.symbol = d.symbol
     AND e.ex_date > d.trade_date - INTERVAL '365 days'
     AND e.ex_date <= d.trade_date
    GROUP BY d.trade_date, d.symbol, d.close
)
SELECT trade_date, symbol, value
FROM calculated
WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
"""
    con.execute(sql)


def _create_volatility_60d(
    con: duckdb.DuckDBPyConnection, symbols: Sequence[str], start: date, end: date
) -> None:
    sql = f"""
CREATE OR REPLACE TEMP TABLE factor_result AS
WITH
{_daily_cte(symbols, end, 'close')},
lagged AS (
    SELECT
        trade_date,
        symbol,
        close,
        lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_close
    FROM daily
),
returns AS (
    SELECT
        trade_date,
        symbol,
        CASE
            WHEN close > 0 AND previous_close > 0 THEN ln(close / previous_close)
            ELSE NULL
        END::DOUBLE AS ret
    FROM lagged
),
windowed AS (
    SELECT
        trade_date,
        symbol,
        count(ret) OVER factor_window AS observation_count,
        stddev_samp(ret) OVER factor_window AS daily_std
    FROM returns
    WINDOW factor_window AS (
        PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
    )
)
SELECT
    trade_date,
    symbol,
    CASE WHEN observation_count = 60 THEN daily_std * sqrt(252.0) ELSE NULL END::DOUBLE AS value
FROM windowed
WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
"""
    con.execute(sql)


def _create_beta_300(
    con: duckdb.DuckDBPyConnection, symbols: Sequence[str], start: date, end: date
) -> None:
    sql = f"""
CREATE OR REPLACE TEMP TABLE factor_result AS
WITH
{_daily_cte(symbols, end, 'close')},
stock_lagged AS (
    SELECT
        trade_date,
        symbol,
        close,
        lag(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS previous_close
    FROM daily
),
stock_returns AS (
    SELECT
        trade_date,
        symbol,
        CASE
            WHEN close > 0 AND previous_close > 0 THEN ln(close / previous_close)
            ELSE NULL
        END::DOUBLE AS stock_ret
    FROM stock_lagged
),
market_daily AS (
    SELECT try_cast(trade_date AS DATE) AS trade_date, max(close) AS close
    FROM read_parquet('{_sql_path(INDEX_DAILY_GLOB)}', hive_partitioning=true)
    WHERE symbol = '000300'
      AND try_cast(trade_date AS DATE) <= DATE '{end.isoformat()}'
    GROUP BY trade_date
),
market_lagged AS (
    SELECT
        trade_date,
        close,
        lag(close) OVER (ORDER BY trade_date) AS previous_close
    FROM market_daily
),
market_returns AS (
    SELECT
        trade_date,
        CASE
            WHEN close > 0 AND previous_close > 0 THEN ln(close / previous_close)
            ELSE NULL
        END::DOUBLE AS market_ret
    FROM market_lagged
),
paired AS (
    SELECT s.trade_date, s.symbol, s.stock_ret, m.market_ret
    FROM stock_returns s
    JOIN market_returns m USING (trade_date)
    WHERE s.stock_ret IS NOT NULL AND m.market_ret IS NOT NULL
),
rolling AS (
    SELECT
        trade_date,
        symbol,
        count(*) OVER factor_window AS observation_count,
        covar_samp(stock_ret, market_ret) OVER factor_window AS covariance,
        var_samp(market_ret) OVER factor_window AS market_variance
    FROM paired
    WINDOW factor_window AS (
        PARTITION BY symbol ORDER BY trade_date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
    )
),
betas AS (
    SELECT
        trade_date,
        symbol,
        CASE
            WHEN observation_count = 252 AND market_variance > 0
            THEN covariance / market_variance
            ELSE NULL
        END::DOUBLE AS value
    FROM rolling
)
SELECT d.trade_date, d.symbol, b.value
FROM daily d
LEFT JOIN betas b USING (trade_date, symbol)
WHERE d.trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
"""
    con.execute(sql)


def _risk_ctes() -> str:
    return f"""
risk_source AS (
    SELECT
        row_number() OVER () AS source_order,
        lpad(regexp_replace(trim(code), '\\.0$', ''), 6, '0') AS symbol,
        try_cast(report_year AS INTEGER) AS report_year,
        try_cast(roe_pct AS DOUBLE) AS roe_pct,
        try_cast(debt_ratio_pct AS DOUBLE) AS debt_ratio_pct
    FROM read_csv('{_sql_path(RISK_CSV_PATH)}', header=true, all_varchar=true)
),
risk_deduplicated AS (
    SELECT symbol, report_year, roe_pct, debt_ratio_pct
    FROM risk_source
    WHERE report_year BETWEEN 1900 AND 2200
    QUALIFY row_number() OVER (
        PARTITION BY symbol, report_year ORDER BY source_order DESC
    ) = 1
),
risk_reports AS (
    SELECT
        symbol,
        report_year,
        make_date(report_year + 1, 4, 30) AS available_date,
        roe_pct,
        debt_ratio_pct
    FROM risk_deduplicated
)
"""


def _prepare_financial_neutralization(
    con: duckdb.DuckDBPyConnection, start: date, end: date
) -> None:
    """Materialize daily full-market industry means for financial factors."""

    sql = f"""
CREATE OR REPLACE TEMP TABLE financial_reports AS
WITH
{_risk_ctes()}
SELECT * FROM risk_reports;

CREATE OR REPLACE TEMP TABLE financial_industry_means AS
WITH
daily_raw AS (
    SELECT try_cast(trade_date AS DATE) AS trade_date, symbol
    FROM read_parquet('{_sql_path(STOCK_DAILY_GLOB)}', hive_partitioning=true)
    WHERE try_cast(trade_date AS DATE)
          BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
),
daily AS (
    SELECT trade_date, symbol
    FROM daily_raw
    WHERE trade_date IS NOT NULL
    GROUP BY trade_date, symbol
),
raw_values AS (
    SELECT d.trade_date, d.symbol, r.roe_pct, r.debt_ratio_pct
    FROM daily d
    ASOF LEFT JOIN financial_reports r
      ON d.symbol = r.symbol AND d.trade_date >= r.available_date
),
classified AS (
    SELECT r.*, i.industry_name
    FROM raw_values r
    LEFT JOIN industry_mapping i USING (symbol)
)
SELECT
    trade_date,
    industry_name,
    avg(roe_pct)::DOUBLE AS roe_mean,
    avg(debt_ratio_pct)::DOUBLE AS debt_ratio_mean
FROM classified
WHERE industry_name IS NOT NULL
GROUP BY trade_date, industry_name
"""
    con.execute(sql)


def _create_financial_factor(
    con: duckdb.DuckDBPyConnection,
    symbols: Sequence[str],
    start: date,
    end: date,
    value_column: str,
) -> None:
    if value_column not in {"roe_pct", "debt_ratio_pct"}:
        raise ValueError(f"不支持的财务字段：{value_column}")
    mean_column = value_column.replace("_pct", "_mean")
    sql = f"""
CREATE OR REPLACE TEMP TABLE factor_result AS
WITH
{_daily_cte(symbols, end, '')},
output_daily AS (
    SELECT trade_date, symbol
    FROM daily
    WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
),
classified AS (
    SELECT d.trade_date, d.symbol, i.industry_name
    FROM output_daily d
    LEFT JOIN industry_mapping i USING (symbol)
),
raw_values AS (
    SELECT d.trade_date, d.symbol, d.industry_name, r.{value_column}::DOUBLE AS raw_value
    FROM classified d
    ASOF LEFT JOIN financial_reports r
      ON d.symbol = r.symbol AND d.trade_date >= r.available_date
)
SELECT
    r.trade_date,
    r.symbol,
    CASE
        WHEN r.raw_value IS NOT NULL AND m.{mean_column} IS NOT NULL
        THEN r.raw_value - m.{mean_column}
        ELSE NULL
    END::DOUBLE AS value
FROM raw_values r
LEFT JOIN financial_industry_means m USING (trade_date, industry_name)
"""
    con.execute(sql)


def _create_roe_volatility(
    con: duckdb.DuckDBPyConnection, symbols: Sequence[str], start: date, end: date
) -> None:
    sql = f"""
CREATE OR REPLACE TEMP TABLE factor_result AS
WITH
{_daily_cte(symbols, end, '')},
{_risk_ctes()},
roe_observations AS (
    SELECT
        r.symbol,
        r.available_date,
        r.roe_pct
    FROM risk_reports r
    WHERE r.roe_pct IS NOT NULL
),
raw_volatility AS (
    SELECT
        symbol,
        available_date,
        CASE
            WHEN count(*) OVER factor_window = {ROE_VOLATILITY_YEARS}
            THEN stddev_samp(roe_pct) OVER factor_window
            ELSE NULL
        END::DOUBLE AS raw_value
    FROM roe_observations
    WINDOW factor_window AS (
        PARTITION BY symbol ORDER BY available_date
        ROWS BETWEEN {ROE_VOLATILITY_YEARS - 1} PRECEDING AND CURRENT ROW
    )
),
output_daily AS (
    SELECT trade_date, symbol
    FROM daily
    WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
)
SELECT d.trade_date, d.symbol, v.raw_value::DOUBLE AS value
FROM output_daily d
ASOF LEFT JOIN raw_volatility v
  ON d.symbol = v.symbol AND d.trade_date >= v.available_date
"""
    con.execute(sql)


def create_factor_result(
    con: duckdb.DuckDBPyConnection,
    factor_name: str,
    symbols: Sequence[str],
    start: date,
    end: date,
) -> None:
    if factor_name == "dividend_yield":
        _create_dividend_yield(con, symbols, start, end)
    elif factor_name == "volatility_60d":
        _create_volatility_60d(con, symbols, start, end)
    elif factor_name == "beta_300":
        _create_beta_300(con, symbols, start, end)
    elif factor_name == "roe":
        _create_financial_factor(con, symbols, start, end, "roe_pct")
    elif factor_name == "debt_ratio":
        _create_financial_factor(con, symbols, start, end, "debt_ratio_pct")
    elif factor_name == "roe_volatility":
        _create_roe_volatility(con, symbols, start, end)
    else:
        raise ValueError(f"未知因子：{factor_name}")


def _partition_complete(factor_name: str, year: int) -> bool:
    files = list((FACTOR_ROOT / factor_name / f"year={year}").glob("part_*.parquet"))
    return bool(files) and all(path.stat().st_size > 0 for path in files)


def _copy_result_year(
    con: duckdb.DuckDBPyConnection,
    stage_factor_dir: Path,
    year: int,
    batch_id: int,
    start: date,
    end: date,
) -> int:
    year_start = max(start, date(year, 1, 1))
    year_end = min(end, date(year, 12, 31))
    row_count = int(
        con.execute(
            "SELECT count(*) FROM factor_result WHERE trade_date BETWEEN ? AND ?",
            [year_start, year_end],
        ).fetchone()[0]
    )
    if row_count == 0:
        return 0

    stage_year = stage_factor_dir / f"year={year}"
    stage_year.mkdir(parents=True, exist_ok=True)
    output_path = stage_year / f"part_{batch_id:04d}.parquet"
    query = f"""
COPY (
    SELECT trade_date::DATE AS trade_date, symbol::VARCHAR AS symbol, value::DOUBLE AS value
    FROM factor_result
    WHERE trade_date BETWEEN DATE '{year_start.isoformat()}' AND DATE '{year_end.isoformat()}'
    ORDER BY trade_date, symbol
) TO '{_sql_path(output_path)}'
(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
"""
    con.execute(query)
    return row_count


def _commit_staged_year(stage_factor_dir: Path, factor_name: str, year: int) -> int:
    stage_year = stage_factor_dir / f"year={year}"
    staged_files = sorted(stage_year.glob("part_*.parquet")) if stage_year.exists() else []
    target_year = FACTOR_ROOT / factor_name / f"year={year}"
    target_year.mkdir(parents=True, exist_ok=True)
    for old_file in target_year.glob("part_*.parquet"):
        old_file.unlink()
    for staged_file in staged_files:
        os.replace(staged_file, target_year / staged_file.name)
    if not staged_files:
        try:
            target_year.rmdir()
        except OSError:
            pass
    return len(staged_files)


def _refresh_manifest_stats(
    con: duckdb.DuckDBPyConnection,
    manifest: dict[str, object],
    factor_name: str,
    computed_at: str,
) -> None:
    config = manifest["factors"][factor_name]  # type: ignore[index]
    assert isinstance(config, dict)
    files = list((FACTOR_ROOT / factor_name).glob("year=*/*.parquet"))
    if not files:
        config.update(
            {"last_computed": computed_at, "row_count": 0, "min_date": None, "max_date": None}
        )
        return
    stats = con.execute(
        f"""
        SELECT
            count(*) AS row_count,
            min(trade_date) FILTER (WHERE value IS NOT NULL) AS min_date,
            max(trade_date) FILTER (WHERE value IS NOT NULL) AS max_date
        FROM read_parquet('{_sql_path(FACTOR_ROOT / factor_name / 'year=*' / '*.parquet')}',
                          hive_partitioning=true)
        """
    ).fetchone()
    config.update(
        {
            "last_computed": computed_at,
            "row_count": int(stats[0]),
            "min_date": stats[1].isoformat() if stats[1] is not None else None,
            "max_date": stats[2].isoformat() if stats[2] is not None else None,
        }
    )


def build_factor(
    con: duckdb.DuckDBPyConnection,
    factor_name: str,
    symbols: Sequence[str],
    years: Sequence[int],
    start: date,
    end: date,
    resume: bool,
    staging_root: Path,
    manifest: dict[str, object],
) -> bool:
    pending_years: list[int] = []
    for year in years:
        if resume and _partition_complete(factor_name, year):
            print(f"跳过 {factor_name}/{year}（已计算）", flush=True)
        else:
            pending_years.append(year)
    if not pending_years:
        return False

    stage_factor_dir = staging_root / factor_name
    stage_factor_dir.mkdir(parents=True, exist_ok=True)
    total_batches = math.ceil(len(symbols) / BATCH_SIZE)
    staged_rows = {year: 0 for year in pending_years}
    for batch_id, batch in _chunks(symbols, BATCH_SIZE):
        print(
            f"计算 {factor_name} 批次 {batch_id + 1}/{total_batches}（{len(batch)} 只）...",
            flush=True,
        )
        create_factor_result(con, factor_name, batch, start, end)
        for year in pending_years:
            staged_rows[year] += _copy_result_year(
                con, stage_factor_dir, year, batch_id, start, end
            )

    for year in pending_years:
        part_count = _commit_staged_year(stage_factor_dir, factor_name, year)
        print(
            f"写入 {factor_name}/year={year}：{staged_rows[year]:,} 行，{part_count} 个 part",
            flush=True,
        )

    computed_at = _utc_now()
    _refresh_manifest_stats(con, manifest, factor_name, computed_at)
    save_manifest(manifest)
    return True


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从本地 Parquet 数据湖构建因子集市")
    parser.add_argument("--factor", required=True, help="因子名，或 all")
    parser.add_argument("--start", type=_parse_date, default=date(2000, 1, 1))
    parser.add_argument("--end", type=_parse_date, default=date.today())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="跳过已有非空分区")
    mode.add_argument("--force", action="store_true", help="强制重算所选年份/日期范围")
    parser.add_argument("--limit", type=int, help="仅处理排序后的前 N 只股票（测试用）")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--start 不能晚于 --end")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit 必须大于 0")
    if args.factor != "all" and args.factor not in FACTOR_NAMES:
        parser.error(f"未知因子 {args.factor!r}；可选：all, {', '.join(FACTOR_NAMES)}")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    factors = list(FACTOR_NAMES) if args.factor == "all" else [args.factor]
    _preflight(factors)
    symbols = load_stock_codes(args.limit)
    if not symbols:
        raise RuntimeError("股票列表为空")
    years = _years_between(args.start, args.end)
    manifest = load_manifest()
    FACTOR_ROOT.mkdir(parents=True, exist_ok=True)
    save_manifest(manifest)

    if args.limit is not None:
        print(f"测试模式：仅处理前 {len(symbols)} 只股票；目标分区会被该子集覆盖。", flush=True)

    staging_root = FACTOR_ROOT / ".staging" / uuid.uuid4().hex
    duckdb_temp = FACTOR_ROOT / ".duckdb_tmp"
    staging_root.mkdir(parents=True, exist_ok=False)
    duckdb_temp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads TO 4")
    con.execute("SET memory_limit = '4GB'")
    con.execute(f"SET temp_directory = '{_sql_path(duckdb_temp)}'")
    con.execute("SET preserve_insertion_order = false")

    industry_mapping = None
    if any(name in {"roe", "debt_ratio"} for name in factors):
        industry_mapping = load_industry_mapping(INDUSTRY_CSV_PATH)
        con.register("industry_mapping", industry_mapping)

    started = datetime.now()
    financial_neutralization_ready = False
    try:
        for factor_name in factors:
            print(f"\n=== {factor_name} ===", flush=True)
            if factor_name in {"roe", "debt_ratio"} and not financial_neutralization_ready:
                print("预计算逐交易日行业均值...", flush=True)
                _prepare_financial_neutralization(con, args.start, args.end)
                financial_neutralization_ready = True
            build_factor(
                con,
                factor_name,
                symbols,
                years,
                args.start,
                args.end,
                args.resume,
                staging_root,
                manifest,
            )
    finally:
        con.close()
        if staging_root.exists():
            shutil.rmtree(staging_root)
        try:
            staging_root.parent.rmdir()
        except OSError:
            pass
        try:
            duckdb_temp.rmdir()
        except OSError:
            pass

    elapsed = datetime.now() - started
    print(f"\n完成，用时 {elapsed}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        raise SystemExit(130)
