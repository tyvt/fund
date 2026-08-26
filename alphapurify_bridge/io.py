"""Persist reproducible diagnosis artifacts and the VectorBT hand-off list."""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
import yaml

from alphapurify_bridge.config import ROOT


def _resolve(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else ROOT / target


def json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def persist_results(results: Sequence[Mapping[str, Any]], output_root: str | Path) -> dict[str, Path]:
    root = _resolve(output_root)
    result_dir = root / "diagnosis_results"
    result_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = json_safe(list(results))
    json_path = result_dir / f"diagnosis_{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    latest = result_dir / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    summary = pd.DataFrame(
        [
            {key: value for key, value in result.items() if key not in {"details", "checks", "official_validation"}}
            for result in results
        ]
    )
    for column in ("ic_by_horizon", "ic_decay", "quantile_returns", "warnings"):
        if column in summary:
            summary[column] = summary[column].map(lambda value: json.dumps(json_safe(value), ensure_ascii=False))
    parquet_path = result_dir / f"diagnosis_{stamp}.parquet"
    summary.to_parquet(parquet_path, index=False)
    return {"json": json_path, "parquet": parquet_path, "latest": latest}


def write_approved_factors(
    results: Sequence[Mapping[str, Any]],
    path: str | Path,
    *,
    diagnosis_artifact: str | Path | None = None,
) -> Path:
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    approved = [str(result["factor_name"]) for result in results if result.get("status") == "PASS"]
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "data_version": results[0].get("data_version") if results else "unknown",
        "alphapurify_version": results[0].get("alphapurify_version") if results else None,
        "diagnosis_artifact": str(diagnosis_artifact) if diagnosis_artifact else None,
        "factors": approved,
    }
    target.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return target


def update_registry_statuses(results: Sequence[Mapping[str, Any]], path: str | Path) -> Path:
    """Record the latest diagnosis timestamp and status for selected factors."""
    target = _resolve(path)
    with target.open("r", encoding="utf-8") as handle:
        registry = yaml.safe_load(handle) or {}
    factors = registry.get("factors")
    if not isinstance(factors, dict):
        raise ValueError(f"因子注册表格式错误：{target}")
    for result in results:
        name = str(result["factor_name"])
        if name not in factors or not isinstance(factors[name], dict):
            raise ValueError(f"因子未注册：{name}")
        factors[name]["last_diagnosed"] = result.get("timestamp")
        factors[name]["diagnosis_status"] = result.get("status")
        factors[name]["diagnosis_data_version"] = result.get("data_version")
    target.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return target


__all__ = ["json_safe", "persist_results", "update_registry_statuses", "write_approved_factors"]
