"""Configuration loading and reproducibility helpers for the VectorBT workbench."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / "config" / "vectorbt"


def deep_merge(base: Mapping[str, Any], overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in (overrides or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_yaml(path: str | Path) -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    with target.open("r", encoding="utf-8") as fh:
        value = yaml.safe_load(fh) or {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML 顶层必须是映射：{target}")
    return value


def load_strategy_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = load_yaml(CONFIG_ROOT / "strategy_params.yaml")
    return deep_merge(raw.get("dividend_lowvol", {}), overrides)


def load_backtest_config(overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    raw = load_yaml(CONFIG_ROOT / "backtest_config.yaml")
    return deep_merge(raw.get("backtest", {}), overrides)


def load_scan_config(path: str | Path | None = None) -> dict[str, Any]:
    raw = load_yaml(path or CONFIG_ROOT / "scan_params.yaml")
    return dict(raw.get("scan", {}))


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def data_version(snapshot_root: str | Path | None = None) -> str:
    root = Path(snapshot_root or ROOT / "data/parquet/factors/snapshots")
    manifest = root / "manifest.json"
    if not manifest.exists():
        return "unknown"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        return stable_hash(payload)[:16]
    except (OSError, ValueError):
        return "unknown"


def reproducibility_snapshot(
    strategy_config: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
    *,
    snapshot_root: str | Path | None = None,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "commit_hash": git_commit(),
        "config_version": stable_hash(
            {"strategy": strategy_config, "backtest": backtest_config}
        )[:16],
        "data_version": data_version(snapshot_root),
    }


def configure_logging(path: str | Path | None = None) -> logging.Logger:
    target = Path(path or ROOT / "logs/vectorbt.log")
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("vectorbt_workbench")
    logger.setLevel(logging.INFO)
    resolved = str(target.resolve())
    if not any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", None) == resolved
        for handler in logger.handlers
    ):
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger
