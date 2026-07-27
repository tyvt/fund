"""历史数据本地缓存：当日已拉取则直接读盘，避免重复请求。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable, TypeVar

import pandas as pd

from config import PROJECT_DIR

CACHE_DIR = PROJECT_DIR / "logs" / "data_cache"
US_CACHE_DIR = PROJECT_DIR / "logs" / "us_index_cache"

T = TypeVar("T")


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
    if not path.exists():
        return None
    try:
        return pd.read_csv(path, parse_dates=parse_dates or ["date"])
    except Exception:
        return None


def save_dataframe(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8")


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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


def get_or_fetch_dataframe(
    key: str,
    fetch_fn: Callable[[], pd.DataFrame | None],
    *,
    subdir: str = "",
    parse_dates: list[str] | None = None,
    force: bool = False,
) -> pd.DataFrame | None:
    path = cache_path(key, ".csv", subdir=subdir)
    if not force and is_fresh_today(path):
        cached = load_dataframe(path, parse_dates=parse_dates)
        if cached is not None and not cached.empty:
            return cached

    frame = fetch_fn()
    if frame is not None and not frame.empty:
        save_dataframe(path, frame)
    return frame


def get_or_fetch_json(
    key: str,
    fetch_fn: Callable[[], T],
    *,
    subdir: str = "",
    force: bool = False,
    fallback_path: Path | None = None,
) -> T:
    path = cache_path(key, ".json", subdir=subdir)
    if not force and is_fresh_today(path):
        cached = load_json(path)
        if cached is not None:
            return cached

    try:
        payload = fetch_fn()
        save_json(path, payload)
        return payload
    except Exception:
        if path.exists():
            cached = load_json(path)
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
    if not force and is_fresh_today(path):
        cached = load_text(path)
        if cached:
            return cached

    try:
        text = fetch_fn()
        if text:
            save_text(path, text)
        return text
    except Exception:
        if path.exists():
            cached = load_text(path)
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
) -> pd.DataFrame | None:
    path = us_cache_path(name if name.endswith(".csv") else f"{name}.csv")
    if not force and is_fresh_today(path):
        cached = load_dataframe(path, parse_dates=parse_dates)
        if cached is not None and not cached.empty:
            return cached

    frame = fetch_fn()
    if frame is not None and not frame.empty:
        save_dataframe(path, frame)
    return frame


def get_or_fetch_us_json(name: str, fetch_fn: Callable[[], T], *, force: bool = False) -> T:
    path = us_cache_path(name)
    if not force and is_fresh_today(path):
        cached = load_json(path)
        if cached is not None:
            return cached
    try:
        payload = fetch_fn()
        save_json(path, payload)
        return payload
    except Exception:
        cached = load_json(path)
        if cached is not None:
            return cached
        raise


def get_or_fetch_us_text(name: str, fetch_fn: Callable[[], str], *, force: bool = False) -> str:
    path = us_cache_path(name)
    if not force and is_fresh_today(path):
        cached = load_text(path)
        if cached:
            return cached
    try:
        text = fetch_fn()
        if text:
            save_text(path, text)
        return text
    except Exception:
        cached = load_text(path)
        if cached:
            return cached
        raise
