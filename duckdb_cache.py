"""CSV 缓存键与 DuckDB 统一时序模型的映射；供 data_cache 双写/回读。"""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from config import MARKET_DUCKDB_PATH, US_DATA_CACHE_DIR
from duckdb_store import (
    ensure_schema,
    load_cn_index_indicator,
    load_kv_snapshot,
    load_wide_frame,
    upsert_cn_index_indicator,
    upsert_kv_snapshot,
    upsert_wide_frame,
)

_ENABLED = True
_DUCKDB_READY: bool | None = None


def set_duckdb_cache_enabled(enabled: bool) -> None:
    global _ENABLED
    _ENABLED = enabled


def ensure_duckdb_cache_ready(*, verbose: bool = False) -> bool:
    """检测 DuckDB 是否可用；不可用时禁用双写/回读，静默走 CSV。"""
    global _ENABLED, _DUCKDB_READY
    if _DUCKDB_READY is not None:
        return _DUCKDB_READY
    if not MARKET_DUCKDB_PATH.exists():
        _ENABLED = False
        _DUCKDB_READY = False
        return False
    try:
        from duckdb_store import get_connection

        conn = get_connection(read_only=True)
        conn.execute("SELECT 1")
        conn.close()
        _DUCKDB_READY = True
        return True
    except Exception as exc:
        _ENABLED = False
        _DUCKDB_READY = False
        if verbose:
            print(f"  DuckDB 不可用，回退 CSV 缓存: {exc}")
        return False


@contextmanager
def _read_connection():
    from duckdb_store import get_connection

    conn = get_connection(read_only=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_connection():
    ensure_schema()
    from duckdb_store import get_connection

    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _parse_index_perf_key(key: str) -> str | None:
    m = re.fullmatch(r"index_perf_(.+)", key)
    return m.group(1) if m else None


def _parse_indicator_key(key: str) -> str | None:
    m = re.fullmatch(r"indicator_(.+)", key)
    return m.group(1) if m else None


_CN_PERF_FIELDS = {
    "close": "close",
    "rolling_pe": "rolling_pe",
    "trading_value": "trading_value",
    "open": "open",
    "high": "high",
    "low": "low",
    "changePct": "change_pct",
    "tradingVol": "trading_vol",
}

_CYB_MAPPINGS: dict[str, tuple[str, str, dict[str, str]]] = {
    "cyb_pe_szse": (
        "cyb_pe_monthly",
        "399006",
        {"index_close": "index_close", "pe": "pe"},
    ),
    "cyb_pb": (
        "cyb_board",
        "399006",
        {"pb": "pb", "pb_equal": "pb_equal", "pb_median": "pb_median"},
    ),
    "cyb_dividend": (
        "cyb_board",
        "399006",
        {"dividend_yield": "dividend_yield"},
    ),
    "cyb_price": (
        "cyb_index",
        "399006",
        {"open": "open", "high": "high", "low": "low", "close": "close", "volume": "volume"},
    ),
}

_US_PRICE_FILES = {
    "ndx_price_akshare.csv": ("ndx", "akshare"),
    "spx_price_akshare.csv": ("spx", "akshare"),
}

_FRED_FILES = {
    "fred_NASDAQ100.csv": "NASDAQ100",
    "fred_SP500.csv": "SP500",
    "fred_DGS10.csv": "DGS10",
}


def infer_cache_params(path: Path) -> tuple[str, str, bool]:
    """从缓存路径推断 (key, subdir, us)。"""
    name = path.stem if path.suffix else path.name
    parent = path.parent.name
    if parent == "us":
        return name, "us", True
    if parent in ("cn", "cyb"):
        return name, parent, False
    return name, "", False


def sync_dataframe_to_duckdb(
    key: str,
    frame: pd.DataFrame,
    *,
    subdir: str = "",
    us: bool = False,
) -> None:
    if not _ENABLED or frame is None or frame.empty:
        return
    try:
        with _write_connection() as conn:
            if subdir == "cn":
                if key == "bond_yield_history":
                    upsert_wide_frame(
                        conn,
                        frame,
                        domain="cn_bond_yield",
                        entity_key="cn",
                        fields={"bond_yield": "bond_yield"},
                        source="eastmoney",
                    )
                    return
                code = _parse_index_perf_key(key)
                if code:
                    upsert_wide_frame(
                        conn,
                        frame,
                        domain="cn_index_perf",
                        entity_key=code,
                        fields=_CN_PERF_FIELDS,
                        source="csindex_api",
                    )
                    return
                code = _parse_indicator_key(key)
                if code:
                    upsert_cn_index_indicator(conn, frame, code)
                    return

            if subdir == "cyb" and key in _CYB_MAPPINGS:
                domain, entity, fields = _CYB_MAPPINGS[key]
                upsert_wide_frame(
                    conn,
                    frame,
                    domain=domain,
                    entity_key=entity,
                    fields=fields,
                    source="akshare",
                )
                return

            if us or subdir == "us":
                name = key if key.endswith(".csv") else f"{key}.csv"
                if name in _US_PRICE_FILES:
                    index_key, source = _US_PRICE_FILES[name]
                    upsert_wide_frame(
                        conn,
                        frame,
                        domain="us_index_daily",
                        entity_key=index_key,
                        fields={"close": "close"},
                        source=source,
                    )
                    return
                if name == "us10y_akshare.csv":
                    upsert_wide_frame(
                        conn,
                        frame,
                        domain="us10y",
                        entity_key="us",
                        fields={"us10y": "us10y"},
                        source="akshare",
                    )
                    return
                if name in _FRED_FILES:
                    series = _FRED_FILES[name]
                    value_col = "value" if "value" in frame.columns else [
                        c for c in frame.columns if c not in ("date", "observation_date")
                    ][0]
                    date_col = "date" if "date" in frame.columns else "observation_date"
                    tmp = frame.rename(columns={date_col: "date", value_col: "value"})
                    if series == "DGS10":
                        tmp["value"] = pd.to_numeric(tmp["value"], errors="coerce") / 100
                    upsert_wide_frame(
                        conn,
                        tmp,
                        domain="fred",
                        entity_key=series,
                        fields={"value": "value"},
                        source="fred",
                    )
    except Exception as exc:
        print(f"  DuckDB 写入跳过 ({key}): {exc}")


def load_dataframe_from_duckdb(
    key: str,
    *,
    subdir: str = "",
    us: bool = False,
) -> pd.DataFrame | None:
    if not _ENABLED or not MARKET_DUCKDB_PATH.exists():
        return None
    try:
        with _read_connection() as conn:
            if subdir == "cn":
                if key == "bond_yield_history":
                    return load_wide_frame(
                        conn,
                        domain="cn_bond_yield",
                        entity_key="cn",
                        fields=["bond_yield"],
                    )
                code = _parse_index_perf_key(key)
                if code:
                    return load_wide_frame(
                        conn,
                        domain="cn_index_perf",
                        entity_key=code,
                        fields=list(_CN_PERF_FIELDS.values()),
                    )
                code = _parse_indicator_key(key)
                if code:
                    return load_cn_index_indicator(conn, code)

            if subdir == "cyb" and key in _CYB_MAPPINGS:
                domain, entity, fields = _CYB_MAPPINGS[key]
                return load_wide_frame(
                    conn,
                    domain=domain,
                    entity_key=entity,
                    fields=list(fields.values()),
                )

            if us or subdir == "us":
                name = key if key.endswith(".csv") else f"{key}.csv"
                if name in _US_PRICE_FILES:
                    index_key, _ = _US_PRICE_FILES[name]
                    return load_wide_frame(
                        conn,
                        domain="us_index_daily",
                        entity_key=index_key,
                        fields=["close"],
                    )
                if name == "us10y_akshare.csv":
                    return load_wide_frame(
                        conn,
                        domain="us10y",
                        entity_key="us",
                        fields=["us10y"],
                    )
                if name in _FRED_FILES:
                    series = _FRED_FILES[name]
                    df = load_wide_frame(
                        conn,
                        domain="fred",
                        entity_key=series,
                        fields=["value"],
                    )
                    if df is None:
                        return None
                    if series == "DGS10":
                        return df.rename(columns={"value": "us10y"})
                    return df.rename(columns={"value": series.lower()})
    except Exception as exc:
        print(f"  DuckDB 读取回退 CSV ({key}): {exc}")
    return None


def sync_json_to_duckdb(key: str, payload, *, subdir: str = "", us: bool = False) -> None:
    if not _ENABLED or payload is None:
        return
    try:
        with _write_connection() as conn:
            snapshot_key = f"{'us' if us else subdir or 'root'}:{key}"
            upsert_kv_snapshot(conn, snapshot_key, payload)

            if us and key.endswith("_forward_pe.json"):
                index_key = "ndx" if key.startswith("ndx") else "spx"
                for pe_kind, field_name in (
                    ("trailing", "trailing"),
                    ("forward", "forward"),
                    ("forwardOwn", "forward_own"),
                ):
                    series = payload.get(pe_kind) or []
                    if not series:
                        continue
                    df = pd.DataFrame(series)
                    if df.empty or "value" not in df.columns:
                        continue
                    df = df.rename(columns={"value": field_name})
                    upsert_wide_frame(
                        conn,
                        df,
                        domain="us_index_pe",
                        entity_key=index_key,
                        fields={field_name: field_name},
                        source="historyofmarket",
                    )
    except Exception as exc:
        print(f"  DuckDB JSON 写入跳过 ({key}): {exc}")


def load_json_from_duckdb(key: str, *, subdir: str = "", us: bool = False):
    if not _ENABLED or not MARKET_DUCKDB_PATH.exists():
        return None
    try:
        with _read_connection() as conn:
            snapshot_key = f"{'us' if us else subdir or 'root'}:{key}"
            return load_kv_snapshot(conn, snapshot_key)
    except Exception:
        return None


def import_csv_tree(cache_root: Path | None = None) -> list[str]:
    """一次性把 cache/cn、cache/cyb、cache/us 导入 DuckDB。"""
    from config import DATA_CACHE_DIR

    root = cache_root or DATA_CACHE_DIR
    lines: list[str] = []
    mappings = [
        (root / "cn", False),
        (root / "cyb", False),
        (root / "us", True),
    ]
    for folder, us in mappings:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.csv")):
            key = path.stem
            try:
                df = pd.read_csv(path)
                sync_dataframe_to_duckdb(key, df, subdir=folder.name, us=us)
                lines.append(f"  {path.name}: {len(df)} 行")
            except Exception as exc:
                lines.append(f"  {path.name}: 失败 {exc}")
        for path in sorted(folder.glob("*.json")):
            try:
                payload = path.read_text(encoding="utf-8")
                import json

                sync_json_to_duckdb(
                    path.name,
                    json.loads(payload),
                    subdir=folder.name,
                    us=us,
                )
                lines.append(f"  {path.name}: JSON 已导入")
            except Exception as exc:
                lines.append(f"  {path.name}: JSON 失败 {exc}")

    if US_DATA_CACHE_DIR.exists() and US_DATA_CACHE_DIR != root / "us":
        for path in sorted(US_DATA_CACHE_DIR.glob("*")):
            if path.suffix not in (".csv", ".json"):
                continue
            try:
                if path.suffix == ".csv":
                    df = pd.read_csv(path)
                    sync_dataframe_to_duckdb(path.name, df, us=True)
                    lines.append(f"  us/{path.name}: {len(df)} 行")
                else:
                    import json

                    sync_json_to_duckdb(
                        path.name,
                        json.loads(path.read_text(encoding="utf-8")),
                        us=True,
                    )
                    lines.append(f"  us/{path.name}: JSON 已导入")
            except Exception as exc:
                lines.append(f"  us/{path.name}: 失败 {exc}")
    return lines
