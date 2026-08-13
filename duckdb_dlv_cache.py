# -*- coding: utf-8 -*-
"""dividend_lowvol 本地缓存 → DuckDB（策略分红/排雷/行业/流动性/qfq K 线）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

from data_cache import load_dataframe
from dividend_lowvol_rotation.config import CACHE_DIR
from dividend_lowvol_rotation.symbols import normalize_stock_code
from duckdb_store import (
    STOCK_QFQ_DOMAIN,
    STOCK_QFQ_FIELDS,
    ensure_schema,
    get_connection,
    upsert_dlv_fhps,
    upsert_dlv_industry,
    upsert_dlv_liquidity,
    upsert_dlv_risk_hist,
    upsert_kv_snapshot,
    upsert_wide_frame,
)

_KLINE_RE = re.compile(r"^kline_(\d{6})(?:_bfq)?$")
_RISK_RE = re.compile(r"^risk_hist_(\d{6})$")
_QUARTER_RE = re.compile(r"^quarter_profit_(\d{6})$")
_LIQ_RE = re.compile(r"^universe_liquidity_(\d{4}-\d{2}-\d{2})$")


def _read_csv(path: Path, *, parse_dates: list[str] | None = None) -> pd.DataFrame | None:
    df = load_dataframe(path, parse_dates=parse_dates or [])
    if df is None or df.empty:
        try:
            df = pd.read_csv(path)
        except Exception:
            return None
    return df if df is not None and not df.empty else None


def import_fhps_tree(cache_dir: Path | None = None, *, conn=None) -> list[str]:
    root = cache_dir or CACHE_DIR
    lines: list[str] = []
    own = conn is None
    if own:
        ensure_schema()
        conn = get_connection()
    frames: list[pd.DataFrame] = []
    for path in sorted(root.glob("fhps_*.csv")):
        if path.stem == "fhps_all_records":
            continue
        df = _read_csv(path, parse_dates=["ex_date"])
        if df is None:
            continue
        if "code" in df.columns:
            df["code"] = df["code"].map(normalize_stock_code)
        frames.append(df)
    merged_path = root / "fhps_all_records.csv"
    if merged_path.exists():
        df = _read_csv(merged_path, parse_dates=["ex_date"])
        if df is not None:
            if "code" in df.columns:
                df["code"] = df["code"].map(normalize_stock_code)
            frames.append(df)
    if frames:
        all_df = pd.concat(frames, ignore_index=True, sort=False)
        n = upsert_dlv_fhps(conn, all_df)
        lines.append(f"  dlv_fhps: {n} 行")
    if own:
        conn.close()
    return lines


def import_risk_tree(cache_dir: Path | None = None, *, conn=None) -> list[str]:
    root = cache_dir or CACHE_DIR
    lines: list[str] = []
    own = conn is None
    if own:
        ensure_schema()
        conn = get_connection()
    frames: list[pd.DataFrame] = []
    merged = root / "risk_hist_merged.csv"
    if merged.exists():
        df = _read_csv(merged)
        if df is not None:
            if "code" in df.columns:
                df["code"] = df["code"].map(normalize_stock_code)
            frames.append(df)
    else:
        for path in sorted(root.glob("risk_hist_*.csv")):
            m = _RISK_RE.match(path.stem)
            if not m:
                continue
            df = _read_csv(path)
            if df is None:
                continue
            if "code" not in df.columns:
                df["code"] = m.group(1)
            df["code"] = df["code"].map(normalize_stock_code)
            frames.append(df)
    if frames:
        all_df = pd.concat(frames, ignore_index=True, sort=False)
        n = upsert_dlv_risk_hist(conn, all_df)
        lines.append(f"  dlv_risk_hist: {n} 行")
    if own:
        conn.close()
    return lines


def import_industry(cache_dir: Path | None = None, *, conn=None) -> list[str]:
    root = cache_dir or CACHE_DIR
    lines: list[str] = []
    own = conn is None
    if own:
        ensure_schema()
        conn = get_connection()
    for name in ("stock_industry_sw_l1.csv", "stock_industry_csrc.csv"):
        path = root / name
        if not path.exists():
            continue
        df = _read_csv(path)
        if df is None:
            continue
        df["code"] = df["code"].map(normalize_stock_code)
        if "source" not in df.columns:
            df["source"] = "sw" if "sw" in name else "csrc"
        n = upsert_dlv_industry(conn, df)
        lines.append(f"  dlv_industry ({name}): {n} 行")
    if own:
        conn.close()
    return lines


def import_liquidity_snapshots(cache_dir: Path | None = None, *, conn=None) -> list[str]:
    root = cache_dir or CACHE_DIR
    lines: list[str] = []
    own = conn is None
    if own:
        ensure_schema()
        conn = get_connection()
    total = 0
    for path in sorted(root.glob("universe_liquidity_*.csv")):
        df = _read_csv(path)
        if df is None or "code" not in df.columns:
            continue
        m = _LIQ_RE.match(path.stem)
        as_of = m.group(1) if m else path.stem.replace("universe_liquidity_", "")
        df = df.copy()
        df["code"] = df["code"].map(normalize_stock_code)
        df["as_of"] = as_of
        total += upsert_dlv_liquidity(conn, df)
    if total:
        lines.append(f"  dlv_liquidity: {total} 行")
    if own:
        conn.close()
    return lines


def import_qfq_kline_tree(cache_dir: Path | None = None, *, conn=None) -> list[str]:
    """导入 cache/dividend_lowvol/kline_*.csv（qfq，无 _bfq 后缀）。"""
    root = cache_dir or CACHE_DIR
    lines: list[str] = []
    own = conn is None
    if own:
        ensure_schema()
        conn = get_connection()
    imported = 0
    for path in sorted(root.glob("kline_*.csv")):
        if path.stem.endswith("_bfq"):
            continue
        m = _KLINE_RE.match(path.stem)
        if not m:
            continue
        code = m.group(1)
        df = _read_csv(path, parse_dates=["date"])
        if df is None or "close" not in df.columns:
            continue
        wide = df[["date", "close"]].copy()
        wide["date"] = pd.to_datetime(wide["date"], errors="coerce")
        wide = wide.dropna(subset=["date", "close"])
        if wide.empty:
            continue
        upsert_wide_frame(
            conn,
            wide,
            domain=STOCK_QFQ_DOMAIN,
            entity_key=code,
            fields={"close": "close"},
            source="csv_qfq",
        )
        imported += 1
    if imported:
        lines.append(f"  {STOCK_QFQ_DOMAIN}: {imported} 只")
    if own:
        conn.close()
    return lines


def import_json_snapshots(cache_dir: Path | None = None, *, conn=None) -> list[str]:
    root = cache_dir or CACHE_DIR
    repo_cache = root.parent
    lines: list[str] = []
    own = conn is None
    if own:
        ensure_schema()
        conn = get_connection()
    candidates = [
        repo_cache / "position_allocation.json",
        root / "position_allocation.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            upsert_kv_snapshot(conn, "dlv:position_allocation", payload)
            lines.append(f"  kv dlv:position_allocation ← {path.name}")
        except Exception as exc:
            lines.append(f"  kv {path.name}: 失败 {exc}")
    if own:
        conn.close()
    return lines


def import_dividend_lowvol_tree(cache_dir: Path | None = None, *, include_qfq_csv: bool = False) -> list[str]:
    """一次性导入 cache/dividend_lowvol 策略缓存。

    include_qfq_csv: 是否导入 kline_*.csv（慢；全市场 qfq 请用 sync_stockdb --qfq）。
    """
    ensure_schema()
    conn = get_connection()
    lines: list[str] = []
    steps = [
        import_fhps_tree,
        import_risk_tree,
        import_industry,
        import_liquidity_snapshots,
        import_json_snapshots,
    ]
    if include_qfq_csv:
        steps.insert(4, import_qfq_kline_tree)
    try:
        for fn in steps:
            lines.extend(fn(cache_dir, conn=conn))
    finally:
        conn.close()
    return lines
