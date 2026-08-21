#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
将 StockDB 与 RQAlpha Bundle 导出为 Parquet 数据湖。

用法：
  python scripts/export_to_parquet.py --scope all --resume
  python scripts/export_to_parquet.py --scope a_share --resume
  python scripts/export_to_parquet.py --limit 100
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import re
import shutil
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pandas as pd
from tqdm import tqdm

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.paths import (
    PARQUET_DIR,
    PARQUET_EXPORT_LOG,
    PROJECT_DIR,
    STOCKDB_HOST,
    STOCKDB_PORT,
    STOCKDB_SDK_PATH,
)
from data.parquet_constants import (
    A_SHARE_CATEGORIES,
    CALENDAR_COLUMNS,
    DEFAULT_INDEX_SYMBOLS,
    INDEX_ORDER_BOOK_IDS,
    DIVIDEND_COLUMNS,
    DOMAIN_EX_CUM_FACTOR,
    DOMAIN_INDEX_DAILY,
    DOMAIN_STOCK_DAILY,
    DOMAIN_STOCK_DAILY_QFQ,
    DOMAIN_STOCK_DIVIDEND,
    DOMAIN_STOCK_SPLIT,
    DOMAIN_STOCK_ST,
    DOMAIN_STOCK_SUSPENDED,
    DOMAIN_STOCK_META,
    DOMAIN_TRADE_CALENDAR,
    EX_CUM_COLUMNS,
    EXPORT_BATCH_SIZE,
    FETCH_RETRIES,
    FETCH_RETRY_DELAY,
    SPLIT_COLUMNS,
    STOCK_DAILY_COLUMNS,
    STOCK_QFQ_COLUMNS,
    SUSPENDED_COLUMNS,
    ST_COLUMNS,
)
from dividend_lowvol_rotation.config import RQALPHA_BUNDLE_PATH
from dividend_lowvol_rotation.rqalpha.symbols_rq import from_rqalpha_id, to_rqalpha_id
from dividend_lowvol_rotation.symbols import normalize_stock_code
from market_data import configure_stdout_utf8


def _setup_logging() -> logging.Logger:
    PARQUET_EXPORT_LOG.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("export_to_parquet")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(PARQUET_EXPORT_LOG, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


LOG = _setup_logging()

PARQUET_EXPORT_ROOT = PARQUET_DIR
STOCK_CODES_CACHE = "stock_codes.json"


def _iso_to_int(d: str) -> int:
    return int(d.replace("-", ""))


def _int_to_iso(value) -> str:
    text = str(int(value))
    if len(text) != 8:
        return str(value)
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _stockdb_date_arg(d: str) -> str:
    """StockDB 范围查询使用 YYYYMMDD 字符串。"""
    return d.replace("-", "")


@contextmanager
def _h5_file(path: Path):
    h5 = h5py.File(str(path), "r")
    try:
        yield h5
    finally:
        h5.close()


def _manifest_path() -> Path:
    return PARQUET_EXPORT_ROOT / "sync_manifest.json"


def _load_manifest() -> dict:
    path = _manifest_path()
    if not path.exists():
        return {"stock_daily": {}, "stock_daily_qfq": {}, "bundle_meta": {}}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        LOG.warning("manifest JSON 损坏，尝试修复：%s", exc)
        repaired = _repair_manifest_from_partial(path)
        if repaired:
            _save_manifest(repaired)
            return repaired
        raise


def _repair_manifest_from_partial(path: Path) -> dict | None:
    """从截断的 manifest 中提取完整股票条目。"""
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r'"(\d{6})": \{\s*"status": "([^"]+)",\s*"rows": (\d+),'
        r'\s*"min_date": "([^"]*)",\s*"max_date": "([^"]*)"\s*\}',
        re.MULTILINE,
    )
    stock_daily: dict = {}
    for match in pattern.finditer(text):
        sym, status, rows, min_d, max_d = match.groups()
        stock_daily[sym] = {
            "status": status,
            "rows": int(rows),
            "min_date": min_d,
            "max_date": max_d,
        }
    if not stock_daily:
        return None
    bak = path.with_suffix(".json.bak")
    if path.exists():
        shutil.copy2(path, bak)
    LOG.info("manifest 已修复：恢复 %d 只股票（备份 %s）", len(stock_daily), bak)
    return {
        "stock_daily": stock_daily,
        "stock_daily_qfq": {},
        "bundle_meta": {"status": "ok", "repaired_at": datetime.now().isoformat()},
    }


def _save_manifest(manifest: dict) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _get_stockdb_client():
    if str(STOCKDB_SDK_PATH) not in sys.path:
        sys.path.insert(0, str(STOCKDB_SDK_PATH))
    from stock_sdk import StockDBClient

    return StockDBClient(host=STOCKDB_HOST, port=STOCKDB_PORT)


def _codes_from_payload(payload: dict, scope: str = "all") -> list[str]:
    keys = payload.keys() if scope == "all" else A_SHARE_CATEGORIES
    codes: list[str] = []
    for key in keys:
        items = payload.get(key)
        if isinstance(items, list):
            codes.extend(str(c) for c in items)
    return sorted(set(codes))


def _save_stock_codes_cache(codes: list[str], scope: str) -> None:
    path = PARQUET_EXPORT_ROOT / DOMAIN_STOCK_META / STOCK_CODES_CACHE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(
            {"codes": codes, "scope": scope, "updated_at": datetime.now().isoformat()},
            f,
            ensure_ascii=False,
        )


def _list_stock_codes_stockdb(scope: str = "all") -> list[str]:
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            if str(STOCKDB_SDK_PATH) not in sys.path:
                sys.path.insert(0, str(STOCKDB_SDK_PATH))
            from stockdb import init

            rd = init(host=STOCKDB_HOST, port=STOCKDB_PORT)
            payload = rd.get("股票代码")
            if payload is None:
                return []
            if not isinstance(payload, dict):
                try:
                    payload = dict(payload)
                except (TypeError, ValueError):
                    return []
            codes = _codes_from_payload(payload, scope)
            if codes:
                _save_stock_codes_cache(codes, scope)
            return codes
        except Exception as exc:
            last_exc = exc
            LOG.warning("StockDB 股票列表第 %d 次失败：%s", attempt + 1, exc)
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_exc or RuntimeError("StockDB 股票列表拉取失败")


def _list_codes_from_cache_file(scope: str = "all") -> list[str]:
    """上次 StockDB 成功拉取后写入的本地缓存。"""
    path = PARQUET_EXPORT_ROOT / DOMAIN_STOCK_META / STOCK_CODES_CACHE
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    cached_scope = data.get("scope") if isinstance(data, dict) else None
    if scope == "all" and cached_scope != "all":
        LOG.warning(
            "stock_codes.json scope=%s，请求 scope=all，跳过缓存（需 StockDB 拉全量列表）",
            cached_scope or "（无）",
        )
        return []
    if cached_scope and cached_scope != scope:
        LOG.warning(
            "stock_codes.json scope=%s 与请求 scope=%s 不一致，跳过缓存",
            cached_scope,
            scope,
        )
        return []
    codes = data.get("codes") if isinstance(data, dict) else None
    if isinstance(codes, list) and codes:
        return sorted({normalize_stock_code(c) for c in codes})
    return []


def _list_codes_from_parquet_securities() -> list[str]:
    """从已导出的 RQAlpha securities.parquet 读取（元数据阶段产物）。"""
    path = PARQUET_EXPORT_ROOT / DOMAIN_STOCK_META / "securities.parquet"
    if not path.exists():
        return []
    df = pd.read_parquet(path, columns=["symbol", "order_book_id"])
    if df.empty:
        return []
    mask = df["symbol"].notna() & df["order_book_id"].notna()
    return sorted({normalize_stock_code(s) for s in df.loc[mask, "symbol"].astype(str)})


def _list_codes_from_bundle(bundle_path: str) -> list[str]:
    for path in (
        PARQUET_EXPORT_ROOT / DOMAIN_STOCK_META / "instruments.pkl",
        Path(bundle_path) / "instruments.pk",
    ):
        if not path.exists():
            continue
        with path.open("rb") as f:
            items = pickle.load(f)
        codes: list[str] = []
        for item in items:
            if item.get("type") != "CS":
                continue
            obid = item.get("order_book_id") or ""
            sym = from_rqalpha_id(str(obid))
            if sym:
                codes.append(sym)
            elif item.get("trading_code"):
                codes.append(normalize_stock_code(item["trading_code"]))
        if codes:
            return sorted(set(codes))
    return []


def _resolve_stock_codes(
    bundle_path: str,
    codes_source: str | None = None,
    scope: str = "all",
) -> list[str]:
    """股票列表仅来自 StockDB 与 RQAlpha（及二者衍生的本地缓存）。"""
    sources: dict[str, callable] = {
        "stockdb": lambda: _list_stock_codes_stockdb(scope),
        "cache": lambda: _list_codes_from_cache_file(scope),
        "parquet_securities": lambda: _list_codes_from_parquet_securities(),
        "bundle": lambda: _list_codes_from_bundle(bundle_path),
    }
    if codes_source and codes_source in sources:
        order = [codes_source]
    else:
        order = ["stockdb", "cache", "parquet_securities", "bundle"]

    errors: list[str] = []
    stockdb_codes: list[str] = []
    bundle_codes: list[str] = []

    for name in order:
        try:
            codes = sources[name]()
            if codes:
                if name == "stockdb":
                    stockdb_codes = codes
                    LOG.info("股票列表来源：stockdb scope=%s（%d 只）", scope, len(codes))
                    break
                LOG.info("股票列表来源：%s（%d 只）", name, len(codes))
                return codes
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            LOG.warning("股票列表来源 %s 失败：%s", name, exc)

    if stockdb_codes:
        try:
            bundle_codes = _list_codes_from_bundle(bundle_path)
        except Exception as exc:
            LOG.warning("合并 RQAlpha 列表失败（仅用 StockDB）：%s", exc)
        if bundle_codes:
            merged = sorted(set(stockdb_codes) | set(bundle_codes))
            LOG.info(
                "股票列表：StockDB %d + RQAlpha 并集 %d 只",
                len(stockdb_codes),
                len(merged),
            )
            return merged
        return stockdb_codes

    raise RuntimeError(
        "无法获取股票列表（仅允许 StockDB / RQAlpha）；"
        + "; ".join(errors[-3:])
    )


def _fetch_kline(
    client,
    codes: list[str],
    start: str,
    end: str,
    *,
    fq: str | None = "bfq",
    fields: str | None = None,
    as_df: bool = False,
) -> dict | pd.DataFrame:
    kwargs: dict = {
        "frequency": "1d",
        "fq": fq,
        "start": _stockdb_date_arg(start),
        "end": _stockdb_date_arg(end),
    }
    if fields:
        kwargs["fields"] = fields
    if as_df:
        kwargs["as_df"] = True
    last_exc: Exception | None = None
    for attempt in range(FETCH_RETRIES):
        try:
            result = client.get_data(codes, **kwargs)
            return result
        except Exception as exc:
            last_exc = exc
            time.sleep(FETCH_RETRY_DELAY * (attempt + 1))
    raise last_exc or RuntimeError("StockDB K 线拉取失败")


def _fetch_kline_split(client, codes: list[str], start: str, end: str, **kwargs) -> dict:
    if not codes:
        return {}
    try:
        return _fetch_kline(client, codes, start, end, **kwargs)
    except Exception:
        if len(codes) <= 1:
            return {}
        mid = len(codes) // 2
        left = _fetch_kline_split(client, codes[:mid], start, end, **kwargs)
        right = _fetch_kline_split(client, codes[mid:], start, end, **kwargs)
        left.update(right)
        return left


def _get_rqalpha_ds(bundle_path: str):
    from rqalpha.data.base_data_source.data_source import BaseDataSource

    return BaseDataSource(SimpleNamespace(data_bundle_path=bundle_path, future_info={}))


def _stockdb_df(records: list[dict], symbol: str) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    if "date" not in df.columns:
        return pd.DataFrame()
    df = df.copy()
    df["symbol"] = normalize_stock_code(symbol)
    df["trade_date"] = df["date"].map(_int_to_iso)
    if "amount" not in df.columns and "turnover" in df.columns:
        pass
    rename = {}
    if "name" in df.columns:
        rename["name"] = "name"
    df = df.rename(columns={"amount": "amount"})
    if "is_st" in df.columns:
        df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").round().astype("Int8")
    df["data_source"] = "stockdb"
    return df


def _stockdb_records_to_df(records: list, fields: str | None = None) -> pd.DataFrame:
    """StockDB 指定 fields 时返回 list 行而非 dict，需补列名。"""
    if not records:
        return pd.DataFrame()
    sample = records[0]
    if isinstance(sample, dict):
        return pd.DataFrame(records)
    if fields:
        names = [f.strip() for f in fields.split(",")]
        return pd.DataFrame(records, columns=names)
    return pd.DataFrame(records)


def _qfq_df_to_export(qdf: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if qdf is None or qdf.empty:
        return pd.DataFrame(columns=STOCK_QFQ_COLUMNS)
    if "date" not in qdf.columns:
        if "trade_date" in qdf.columns and "close" in qdf.columns:
            out = qdf.copy()
            out["symbol"] = normalize_stock_code(symbol)
            out["data_source"] = "stockdb"
            return out[STOCK_QFQ_COLUMNS]
        return pd.DataFrame(columns=STOCK_QFQ_COLUMNS)
    nc = normalize_stock_code(symbol)
    out = pd.DataFrame(
        {
            "trade_date": qdf["date"].map(_int_to_iso),
            "symbol": nc,
            "close": pd.to_numeric(qdf["close"], errors="coerce"),
            "data_source": "stockdb",
        }
    )
    return out.dropna(subset=["trade_date", "close"])


def _rq_bars_df(bars, symbol: str) -> pd.DataFrame:
    if bars is None or len(bars) == 0:
        return pd.DataFrame()
    from rqalpha.utils.datetime_func import convert_int_to_date

    rows = []
    for bar in bars:
        dt = convert_int_to_date(int(bar["datetime"]))
        row = {
            "trade_date": dt.strftime("%Y-%m-%d"),
            "symbol": normalize_stock_code(symbol),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar["volume"]),
            "amount": float(bar["total_turnover"]),
            "data_source": "rqalpha",
        }
        if "limit_up" in bar.dtype.names:
            row["limit_up"] = float(bar["limit_up"])
            row["limit_down"] = float(bar["limit_down"])
        rows.append(row)
    return pd.DataFrame(rows)


def _append_rq_only_dates(merged: pd.DataFrame, rq_only: pd.DataFrame) -> pd.DataFrame:
    """将仅 RQAlpha 有的交易日并入，避免 reindex+concat 触发 FutureWarning。"""
    if rq_only.empty:
        return merged
    symbol = str(merged["symbol"].iloc[0])
    fill_from_rq = (
        "open", "high", "low", "close", "volume", "amount", "limit_up", "limit_down", "trade_date"
    )
    rows: list[dict] = []
    for row in rq_only.itertuples(index=False):
        rec = {col: np.nan for col in STOCK_DAILY_COLUMNS}
        rec["trade_date"] = row.trade_date
        rec["symbol"] = symbol
        rec["data_source"] = "rqalpha"
        for col in fill_from_rq:
            if col not in STOCK_DAILY_COLUMNS:
                continue
            val = getattr(row, col, np.nan)
            if pd.notna(val):
                rec[col] = val
        rows.append(rec)
    extra = pd.DataFrame(rows, columns=STOCK_DAILY_COLUMNS)
    for col in STOCK_DAILY_COLUMNS:
        if col not in merged.columns:
            merged[col] = np.nan
    left = merged[STOCK_DAILY_COLUMNS]
    combined_records = left.to_dict("records") + extra.to_dict("records")
    return pd.DataFrame(combined_records, columns=STOCK_DAILY_COLUMNS)


def _merge_daily(stockdb_df: pd.DataFrame, rq_df: pd.DataFrame) -> pd.DataFrame:
    base_cols = [
        "open", "high", "low", "close", "pre_close", "volume", "amount", "turnover",
        "pct_chg", "amplitude", "vol_ratio", "pe_ttm", "pb", "total_mv", "float_mv",
        "total_share", "float_share", "is_st",
    ]
    if stockdb_df.empty and rq_df.empty:
        return pd.DataFrame(columns=STOCK_DAILY_COLUMNS)

    if stockdb_df.empty:
        merged = rq_df.copy()
        for c in base_cols:
            if c not in merged.columns:
                merged[c] = np.nan
        merged["data_source"] = "rqalpha"
    else:
        merged = stockdb_df.copy()
        if not rq_df.empty:
            rq_lim = rq_df[["trade_date", "limit_up", "limit_down"]].drop_duplicates("trade_date")
            merged = merged.merge(rq_lim, on="trade_date", how="left")
            rq_only = rq_df.loc[~rq_df["trade_date"].isin(merged["trade_date"])].copy()
            if not rq_only.empty:
                merged = _append_rq_only_dates(merged, rq_only)
        else:
            merged["limit_up"] = np.nan
            merged["limit_down"] = np.nan

    for col in STOCK_DAILY_COLUMNS:
        if col not in merged.columns:
            merged[col] = np.nan
    merged = merged.sort_values("trade_date").drop_duplicates("trade_date", keep="first")
    return merged[STOCK_DAILY_COLUMNS]


def _domain_dir(domain: str) -> Path:
    return PARQUET_EXPORT_ROOT / domain


def _coerce_daily_export_frame(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """统一 Parquet 列类型，避免 object/bool/float 混写失败。"""
    out = df[columns].copy()
    numeric_cols = [
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "amount",
        "turnover",
        "pct_chg",
        "amplitude",
        "vol_ratio",
        "pe_ttm",
        "pb",
        "total_mv",
        "float_mv",
        "total_share",
        "float_share",
        "limit_up",
        "limit_down",
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    if "is_st" in out.columns:
        out["is_st"] = pd.to_numeric(out["is_st"], errors="coerce").round().astype("Int8")
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str)
    if "trade_date" in out.columns:
        out["trade_date"] = out["trade_date"].astype(str)
    if "data_source" in out.columns:
        out["data_source"] = out["data_source"].astype(str)
    return out


def _write_year_partitions(df: pd.DataFrame, domain: str, part_id: int, columns: list[str]) -> int:
    if df.empty:
        return 0
    out_dir = _domain_dir(domain)
    df = df.copy()
    df["year"] = pd.to_datetime(df["trade_date"]).dt.year
    total = 0
    for year, chunk in df.groupby("year"):
        year_dir = out_dir / f"year={int(year)}"
        year_dir.mkdir(parents=True, exist_ok=True)
        path = year_dir / f"part_{part_id:04d}.parquet"
        chunk = _coerce_daily_export_frame(chunk, columns)
        chunk.to_parquet(path, index=False)
        total += len(chunk)
    return total


def export_bundle_metadata(bundle_path: str, manifest: dict, force: bool = False) -> None:
    if manifest.get("bundle_meta", {}).get("status") == "ok" and not force:
        LOG.info("bundle 元数据已导出，跳过")
        return

    LOG.info("导出 bundle 元数据：%s", bundle_path)
    bundle = Path(bundle_path)

    # 交易日历
    cal_path = _domain_dir(DOMAIN_TRADE_CALENDAR) / "calendar.parquet"
    cal_path.parent.mkdir(parents=True, exist_ok=True)
    dates = np.load(bundle / "trading_dates.npy", allow_pickle=False)
    cal_df = pd.DataFrame({"trade_date": [_int_to_iso(d) for d in dates]})
    cal_df.to_parquet(cal_path, index=False)

    # instruments.pkl（RQAlpha 回测直接加载）
    meta_dir = _domain_dir(DOMAIN_STOCK_META)
    meta_dir.mkdir(parents=True, exist_ok=True)
    with (bundle / "instruments.pk").open("rb") as f:
        instruments = pickle.load(f)
    with (meta_dir / "instruments.pkl").open("wb") as f:
        pickle.dump(instruments, f)

    # securities.parquet（宽表元数据）
    sec_rows = []
    for item in instruments:
        if item.get("type") not in ("CS", "INDX"):
            continue
        sym = from_rqalpha_id(item["order_book_id"])
        sec_rows.append(
            {
                "symbol": sym,
                "order_book_id": item.get("order_book_id"),
                "name": item.get("symbol"),
                "exchange": item.get("exchange"),
                "listed_date": item.get("listed_date"),
                "de_listed_date": item.get("de_listed_date"),
                "round_lot": item.get("round_lot"),
                "board_type": item.get("board_type"),
                "instrument_json": json.dumps(item, ensure_ascii=False),
            }
        )
    pd.DataFrame(sec_rows).to_parquet(meta_dir / "securities.parquet", index=False)

    # 分红
    div_rows = []
    with _h5_file(bundle / "dividends.h5") as h5:
        for obid in h5.keys():
            sym = from_rqalpha_id(obid)
            arr = h5[obid][:]
            for row in arr:
                div_rows.append(
                    {
                        "symbol": sym,
                        "order_book_id": obid,
                        "book_closure_date": int(row["book_closure_date"]),
                        "announcement_date": int(row["announcement_date"]),
                        "dividend_cash_before_tax": float(row["dividend_cash_before_tax"]),
                        "ex_dividend_date": int(row["ex_dividend_date"]),
                        "payable_date": int(row["payable_date"]),
                        "round_lot": float(row["round_lot"]),
                    }
                )
    div_dir = _domain_dir(DOMAIN_STOCK_DIVIDEND)
    div_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(div_rows, columns=DIVIDEND_COLUMNS).to_parquet(
        div_dir / "dividend_events.parquet", index=False
    )

    # 送股
    split_rows = []
    with _h5_file(bundle / "split_factor.h5") as h5:
        for obid in h5.keys():
            sym = from_rqalpha_id(obid)
            for row in h5[obid][:]:
                split_rows.append(
                    {
                        "symbol": sym,
                        "order_book_id": obid,
                        "ex_date": int(row["ex_date"]),
                        "split_factor": float(row["split_factor"]),
                    }
                )
    split_dir = _domain_dir(DOMAIN_STOCK_SPLIT)
    split_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(split_rows, columns=SPLIT_COLUMNS).to_parquet(
        split_dir / "split_events.parquet", index=False
    )

    # 累计除权因子
    ex_rows = []
    with _h5_file(bundle / "ex_cum_factor.h5") as h5:
        for obid in h5.keys():
            for row in h5[obid][:]:
                ex_rows.append(
                    {
                        "order_book_id": obid,
                        "start_date": int(row["start_date"]),
                        "ex_cum_factor": float(row["ex_cum_factor"]),
                    }
                )
    ex_dir = _domain_dir(DOMAIN_EX_CUM_FACTOR)
    ex_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(ex_rows, columns=EX_CUM_COLUMNS).to_parquet(
        ex_dir / "ex_cum_factor.parquet", index=False
    )

    # 停牌
    sus_rows = []
    with _h5_file(bundle / "suspended_days.h5") as h5:
        for obid in h5.keys():
            for d in h5[obid][:]:
                sus_rows.append({"order_book_id": obid, "suspend_date": int(d)})
    sus_dir = _domain_dir(DOMAIN_STOCK_SUSPENDED)
    sus_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(sus_rows, columns=SUSPENDED_COLUMNS).to_parquet(
        sus_dir / "suspended_days.parquet", index=False
    )

    # ST
    st_rows = []
    with _h5_file(bundle / "st_stock_days.h5") as h5:
        for obid in h5.keys():
            for d in h5[obid][:]:
                st_rows.append({"order_book_id": obid, "st_date": int(d)})
    st_dir = _domain_dir(DOMAIN_STOCK_ST)
    st_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(st_rows, columns=ST_COLUMNS).to_parquet(st_dir / "st_days.parquet", index=False)

    # 指数日 K（benchmark）
    ds = _get_rqalpha_ds(bundle_path)
    idx_frames = []
    for sym in DEFAULT_INDEX_SYMBOLS:
        obid = INDEX_ORDER_BOOK_IDS.get(sym) or to_rqalpha_id(sym)
        if not obid:
            continue
        try:
            inst = next(ds.get_instruments([obid]))
        except StopIteration:
            continue
        bars = ds.history_bars(
            inst,
            None,
            "1d",
            ["datetime", "open", "high", "low", "close", "volume", "total_turnover"],
            datetime.fromisoformat("2099-12-31"),
            skip_suspended=False,
            include_now=True,
            adjust_type="none",
        )
        df = _rq_bars_df(bars, sym)
        if not df.empty:
            idx_frames.append(df)
    if idx_frames:
        idx_df = pd.concat(idx_frames, ignore_index=True)
        _write_year_partitions(idx_df, DOMAIN_INDEX_DAILY, 0, ["trade_date", "symbol", "open", "high", "low", "close", "volume", "amount"])

    manifest["bundle_meta"] = {
        "status": "ok",
        "exported_at": datetime.now().isoformat(),
        "dividend_rows": len(div_rows),
        "split_rows": len(split_rows),
    }
    _save_manifest(manifest)
    LOG.info("bundle 元数据导出完成")


def export_stock_batch(
    codes: list[str],
    part_id: int,
    start: str,
    end: str,
    bundle_path: str,
    manifest: dict,
    resume: bool,
) -> list[dict]:
    client = _get_stockdb_client()
    ds = _get_rqalpha_ds(bundle_path)
    end_dt = datetime.fromisoformat(end[:10])

    daily_frames: list[pd.DataFrame] = []
    qfq_frames: list[pd.DataFrame] = []
    report: list[dict] = []

    codes_to_fetch = []
    for code in codes:
        if resume and manifest.get("stock_daily", {}).get(code, {}).get("status") == "ok":
            continue
        codes_to_fetch.append(code)

    if not codes_to_fetch:
        LOG.info("批次 %d：全部 %d 只已在 manifest 中，跳过", part_id, len(codes))
        return report

    LOG.info(
        "批次 %d：待拉取 %d/%d 只（bfq 区间 %s ~ %s）",
        part_id,
        len(codes_to_fetch),
        len(codes),
        start,
        end,
    )

    LOG.info("批次 %d：StockDB bfq 批量拉取…", part_id)
    kline_dict = _fetch_kline_split(client, codes_to_fetch, start, end, fq="bfq")
    LOG.info("批次 %d：bfq 完成，收到 %d 只", part_id, len(kline_dict))

    qfq_map: dict[str, pd.DataFrame] = {}
    qfq_chunk = 10
    for i in range(0, len(codes_to_fetch), qfq_chunk):
        chunk = codes_to_fetch[i : i + qfq_chunk]
        LOG.info(
            "批次 %d：qfq 进度 %d/%d",
            part_id,
            min(i + qfq_chunk, len(codes_to_fetch)),
            len(codes_to_fetch),
        )
        try:
            qfq_raw = _fetch_kline_split(
                client, chunk, start, end, fq="qfq", fields="date,code,close"
            )
            for sym, records in qfq_raw.items():
                if records:
                    qfq_map[normalize_stock_code(sym)] = _stockdb_records_to_df(
                        records, "date,code,close"
                    )
        except Exception as exc:
            LOG.warning("qfq 批量失败，回退单只：%s", exc)
            for code in chunk:
                try:
                    qfq_one = _fetch_kline(
                        client, [code], start, end, fq="qfq", fields="date,code,close", as_df=True
                    )
                    if isinstance(qfq_one, pd.DataFrame) and not qfq_one.empty:
                        qfq_map[normalize_stock_code(code)] = qfq_one
                except Exception as exc2:
                    LOG.warning("前复权拉取失败 %s: %s", code, exc2)

    for code in tqdm(codes_to_fetch, desc=f"批次{part_id}合并", leave=False):
        nc = normalize_stock_code(code)
        sdb_records = kline_dict.get(nc) or kline_dict.get(code) or []
        sdb_df = _stockdb_df(sdb_records, nc)

        obid = to_rqalpha_id(nc)
        rq_df = pd.DataFrame()
        if obid:
            try:
                inst = next(ds.get_instruments([obid]))
                bars = ds.history_bars(
                    inst,
                    None,
                    "1d",
                    ["datetime", "open", "high", "low", "close", "volume", "total_turnover", "limit_up", "limit_down"],
                    end_dt,
                    skip_suspended=False,
                    include_now=True,
                    adjust_type="none",
                )
                rq_df = _rq_bars_df(bars, nc)
            except StopIteration:
                pass

        merged = _merge_daily(sdb_df, rq_df)
        if merged.empty:
            LOG.warning("无日 K 数据：%s", nc)
            manifest.setdefault("stock_daily", {})[nc] = {"status": "empty", "rows": 0}
            continue

        daily_frames.append(merged)

        qfq_part = qfq_map.get(nc)
        if qfq_part is not None and not qfq_part.empty:
            qfq_export = _qfq_df_to_export(qfq_part, nc)
            if not qfq_export.empty:
                qfq_frames.append(qfq_export)
                manifest.setdefault("stock_daily_qfq", {})[nc] = {
                    "status": "ok",
                    "rows": len(qfq_export),
                    "min_date": str(qfq_export["trade_date"].min()),
                    "max_date": str(qfq_export["trade_date"].max()),
                }

        manifest.setdefault("stock_daily", {})[nc] = {
            "status": "ok",
            "rows": len(merged),
            "min_date": merged["trade_date"].min(),
            "max_date": merged["trade_date"].max(),
        }
        report.append(manifest["stock_daily"][nc].copy())
        report[-1]["symbol"] = nc
        _save_manifest(manifest)
        LOG.info("批次 %d：%s 完成 %d 行", part_id, nc, len(merged))

    if daily_frames:
        all_daily = pd.concat(daily_frames, ignore_index=True)
        _write_year_partitions(all_daily, DOMAIN_STOCK_DAILY, part_id, STOCK_DAILY_COLUMNS)

    if qfq_frames:
        all_qfq = pd.concat(qfq_frames, ignore_index=True)
        _write_year_partitions(all_qfq, DOMAIN_STOCK_DAILY_QFQ, part_id, STOCK_QFQ_COLUMNS)

    _save_manifest(manifest)
    return report


def _report_rows_from_manifest(manifest: dict) -> list[dict]:
    rows: list[dict] = []
    for symbol, meta in sorted(manifest.get("stock_daily", {}).items()):
        if not isinstance(meta, dict) or meta.get("status") != "ok":
            continue
        row = dict(meta)
        row["symbol"] = symbol
        rows.append(row)
    return rows


def write_sync_report(report_rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Parquet 同步报告",
        "",
        f"> 生成时间：{datetime.now().isoformat()}",
        "",
        "| symbol | rows | min_date | max_date |",
        "|--------|------|----------|----------|",
    ]
    for row in report_rows:
        lines.append(
            f"| {row.get('symbol', '')} | {row.get('rows', 0)} | {row.get('min_date', '')} | {row.get('max_date', '')} |"
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    configure_stdout_utf8()
    parser = argparse.ArgumentParser(description="StockDB + RQAlpha → Parquet 数据湖")
    parser.add_argument("--start", default="2000-01-01")
    parser.add_argument("--end", default=date.today().isoformat())
    parser.add_argument("--output", default=str(PARQUET_DIR))
    parser.add_argument("--bundle", default=RQALPHA_BUNDLE_PATH)
    parser.add_argument("--batch-size", type=int, default=EXPORT_BATCH_SIZE, help="每批股票数（试点建议 20）")
    parser.add_argument("--limit", type=int, default=None, help="仅导出前 N 只股票（试点）")
    parser.add_argument("--symbols-file", default=None, help="指定股票列表文件")
    parser.add_argument("--resume", action="store_true", help="跳过 manifest 中已完成的股票")
    parser.add_argument("--force-meta", action="store_true", help="强制重新导出 bundle 元数据")
    parser.add_argument("--skip-meta", action="store_true", help="跳过 bundle 元数据导出")
    parser.add_argument(
        "--scope",
        default="all",
        choices=["all", "a_share"],
        help="股票列表范围：all=StockDB 全量 0/1/3/5/6/9（约 7563）；a_share=仅 0/3/6/9 A 股",
    )
    parser.add_argument(
        "--codes-source",
        default=None,
        choices=["stockdb", "cache", "parquet_securities", "bundle"],
        help="强制股票列表来源（默认 StockDB→本地缓存→RQAlpha securities→bundle）",
    )
    parser.add_argument(
        "--report",
        default=str(PROJECT_DIR / "output" / "parquet_sync_report.md"),
        help="同步报告输出路径",
    )
    args = parser.parse_args(argv)

    global PARQUET_EXPORT_ROOT
    PARQUET_EXPORT_ROOT = Path(args.output)
    PARQUET_EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    os.environ["DLV_PARQUET_ROOT"] = str(PARQUET_EXPORT_ROOT)

    manifest = _load_manifest()
    if not args.skip_meta:
        export_bundle_metadata(args.bundle, manifest, force=args.force_meta)

    if args.symbols_file:
        codes = [
            normalize_stock_code(line.strip())
            for line in Path(args.symbols_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        scope = "all" if args.scope == "all" else "a_share"
        codes = _resolve_stock_codes(args.bundle, args.codes_source, scope=scope)

    if args.limit:
        codes = codes[: args.limit]

    LOG.info("股票数量：%d，区间 %s ~ %s，scope=%s", len(codes), args.start, args.end, args.scope)

    all_report: list[dict] = []
    batches = [
        codes[i : i + args.batch_size] for i in range(0, len(codes), args.batch_size)
    ]
    for part_id, batch in enumerate(tqdm(batches, desc="导出批次")):
        rows = export_stock_batch(
            batch,
            part_id,
            args.start,
            args.end,
            args.bundle,
            manifest,
            args.resume,
        )
        all_report.extend(rows)

    if not all_report:
        all_report = _report_rows_from_manifest(manifest)
        if all_report:
            LOG.info("本轮无新导出，从 manifest 生成报告（%d 只）", len(all_report))

    write_sync_report(all_report, Path(args.report))
    LOG.info("同步完成，报告：%s", args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
