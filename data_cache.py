"""历史数据本地缓存：持久化存储，过期后增量合并，供回测与报告共用。"""

from __future__ import annotations

import json
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Callable, TypeVar

import pandas as pd

from config import DATA_CACHE_DIR, US_DATA_CACHE_DIR

CACHE_DIR = DATA_CACHE_DIR
US_CACHE_DIR = US_DATA_CACHE_DIR

T = TypeVar("T")

# 单次报告/推送进程内的 snapshot 记忆，避免额度分配与报告重复构建
_RUN_MEMO: dict[str, object] = {}
_RUN_MEMO_LOCK = threading.Lock()


def run_memo(key: str, factory: Callable[[], T]) -> T:
    """进程内按 key 记忆计算结果；factory 仅在首次调用。"""
    with _RUN_MEMO_LOCK:
        if key in _RUN_MEMO:
            return _RUN_MEMO[key]  # type: ignore[return-value]
    value = factory()
    with _RUN_MEMO_LOCK:
        existing = _RUN_MEMO.get(key)
        if existing is not None:
            return existing  # type: ignore[return-value]
        _RUN_MEMO[key] = value
        return value


def clear_run_memo() -> None:
    with _RUN_MEMO_LOCK:
        _RUN_MEMO.clear()


def _safe_name(key: str) -> str:
    return re.sub(r"[^\w.-]+", "_", key.strip())


def cache_path(key: str, ext: str = ".csv", *, subdir: str = "") -> Path:
    base = CACHE_DIR / subdir if subdir else CACHE_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{_safe_name(key)}{ext}"


def us_cache_path(name: str) -> Path:
    US_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return US_CACHE_DIR / name


def is_fresh_today(path: Path) -> bool:
    """缓存文件修改日期为今天则视为有效。"""
    if not path.exists():
        return False
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return modified.date() == date.today()


def load_dataframe(path: Path, parse_dates: list[str] | None = None) -> pd.DataFrame | None:
    from duckdb_cache import ensure_duckdb_cache_ready, infer_cache_params, load_dataframe_from_duckdb

    ensure_duckdb_cache_ready()

    key, subdir, us = infer_cache_params(path)
    try:
        cached = load_dataframe_from_duckdb(key, subdir=subdir, us=us)
        if cached is not None and not cached.empty:
            if parse_dates:
                for col in parse_dates:
                    if col in cached.columns:
                        cached[col] = pd.to_datetime(cached[col])
            return cached
    except Exception:
        pass

    if not path.exists():
        return None
    try:
        return pd.read_csv(path, parse_dates=parse_dates or ["date"])
    except Exception:
        return None


def save_dataframe(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")
    from duckdb_cache import infer_cache_params, sync_dataframe_to_duckdb

    key, subdir, us = infer_cache_params(path)
    try:
        sync_dataframe_to_duckdb(key, frame, subdir=subdir, us=us)
    except Exception:
        pass


def load_json(path: Path):
    from duckdb_cache import infer_cache_params, load_json_from_duckdb

    key, subdir, us = infer_cache_params(path)
    try:
        from duckdb_cache import load_json_from_duckdb

        cached = load_json_from_duckdb(key, subdir=subdir, us=us)
        if cached is not None:
            return cached
    except Exception:
        pass

    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    from duckdb_cache import infer_cache_params, sync_json_to_duckdb

    key, subdir, us = infer_cache_params(path)
    try:
        sync_json_to_duckdb(key, payload, subdir=subdir, us=us)
    except Exception:
        pass


def load_text(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _normalize_date_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, errors="coerce").dt.normalize().astype("datetime64[ns]")


def merge_dataframes_by_date(
    cached: pd.DataFrame | None,
    fresh: pd.DataFrame | None,
    *,
    date_col: str = "date",
) -> pd.DataFrame | None:
    """按日期列合并去重，新数据覆盖同日旧值。"""
    if cached is None or cached.empty:
        return fresh.copy() if fresh is not None else None
    if fresh is None or fresh.empty:
        return cached.copy()

    left = cached.copy()
    right = fresh.copy()
    if date_col not in left.columns or date_col not in right.columns:
        combined = pd.concat([left, right], ignore_index=True)
        return combined.drop_duplicates(keep="last").reset_index(drop=True)

    left["_merge_dt"] = _normalize_date_series(left[date_col])
    right["_merge_dt"] = _normalize_date_series(right[date_col])
    left = left.dropna(subset=["_merge_dt"])
    right = right.dropna(subset=["_merge_dt"])

    combined = pd.concat([left, right], ignore_index=True)
    combined = combined.sort_values("_merge_dt")
    combined = combined.drop_duplicates(subset=["_merge_dt"], keep="last")
    # 保留 datetime64，供 merge_asof 等对齐；勿转为 python date（object dtype 会触发 pandas 报错）
    combined[date_col] = combined["_merge_dt"]
    combined = combined.drop(columns=["_merge_dt"], errors="ignore")
    return combined.reset_index(drop=True)


def get_or_fetch_dataframe(
    key: str,
    fetch_fn: Callable[[], pd.DataFrame | None],
    *,
    subdir: str = "",
    parse_dates: list[str] | None = None,
    force: bool = False,
    date_col: str = "date",
) -> pd.DataFrame | None:
    path = cache_path(key, ".csv", subdir=subdir)
    cached = load_dataframe(path, parse_dates=parse_dates)

    if not force and is_fresh_today(path) and cached is not None and not cached.empty:
        return cached

    try:
        fresh = fetch_fn()
    except Exception:
        if cached is not None and not cached.empty:
            return cached
        raise

    if fresh is None or fresh.empty:
        return cached

    merged = merge_dataframes_by_date(cached, fresh, date_col=date_col)
    if merged is not None and not merged.empty:
        save_dataframe(path, merged)
    return merged


def get_or_fetch_json(
    key: str,
    fetch_fn: Callable[[], T],
    *,
    subdir: str = "",
    force: bool = False,
    fallback_path: Path | None = None,
) -> T:
    path = cache_path(key, ".json", subdir=subdir)
    cached = load_json(path)
    if not force and is_fresh_today(path) and cached is not None:
        return cached

    try:
        payload = fetch_fn()
        save_json(path, payload)
        return payload
    except Exception:
        if cached is not None:
            return cached
        if fallback_path and fallback_path.exists():
            cached = load_json(fallback_path)
            if cached is not None:
                return cached
        raise


def get_or_fetch_text(
    key: str,
    fetch_fn: Callable[[], str],
    *,
    subdir: str = "",
    ext: str = ".csv",
    force: bool = False,
    fallback_path: Path | None = None,
) -> str:
    path = cache_path(key, ext, subdir=subdir)
    cached = load_text(path)
    if not force and is_fresh_today(path) and cached:
        return cached

    try:
        text = fetch_fn()
        if text:
            save_text(path, text)
        return text
    except Exception:
        if cached:
            return cached
        if fallback_path and fallback_path.exists():
            cached = load_text(fallback_path)
            if cached:
                return cached
        raise


def get_or_fetch_us_dataframe(
    name: str,
    fetch_fn: Callable[[], pd.DataFrame | None],
    *,
    parse_dates: list[str] | None = None,
    force: bool = False,
    date_col: str = "date",
) -> pd.DataFrame | None:
    path = us_cache_path(name if name.endswith(".csv") else f"{name}.csv")
    cached = load_dataframe(path, parse_dates=parse_dates)

    if not force and is_fresh_today(path) and cached is not None and not cached.empty:
        return cached

    try:
        fresh = fetch_fn()
    except Exception:
        if cached is not None and not cached.empty:
            return cached
        raise

    if fresh is None or fresh.empty:
        return cached

    merged = merge_dataframes_by_date(cached, fresh, date_col=date_col)
    if merged is not None and not merged.empty:
        save_dataframe(path, merged)
    return merged


def get_or_fetch_us_json(name: str, fetch_fn: Callable[[], T], *, force: bool = False) -> T:
    path = us_cache_path(name)
    cached = load_json(path)
    if not force and is_fresh_today(path) and cached is not None:
        return cached
    try:
        payload = fetch_fn()
        save_json(path, payload)
        return payload
    except Exception:
        if cached is not None:
            return cached
        raise


def get_or_fetch_us_text(name: str, fetch_fn: Callable[[], str], *, force: bool = False) -> str:
    path = us_cache_path(name)
    cached = load_text(path)
    if not force and is_fresh_today(path) and cached:
        return cached
    try:
        text = fetch_fn()
        if text:
            save_text(path, text)
        return text
    except Exception:
        if cached:
            return cached
        raise
