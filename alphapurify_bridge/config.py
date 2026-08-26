"""Configuration, registry, and reproducibility helpers."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "alphapurify" / "diagnosis_config.yaml"
DEFAULT_REGISTRY_PATH = ROOT / "config" / "alphapurify" / "factor_registry.yaml"


def _resolve(path: str | Path) -> Path:
    target = Path(path)
    return target if target.is_absolute() else ROOT / target


def load_yaml(path: str | Path) -> dict[str, Any]:
    target = _resolve(path)
    with target.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML 顶层必须是映射：{target}")
    return value


def deep_merge(base: Mapping[str, Any], overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in (overrides or {}).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_diagnosis_config(
    path: str | Path = DEFAULT_CONFIG_PATH,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = deep_merge(load_yaml(path), overrides)
    diagnosis = config.setdefault("diagnosis", {})
    thresholds = config.setdefault("thresholds", {})
    output = config.setdefault("output", {})
    if not isinstance(diagnosis, dict) or not isinstance(thresholds, dict) or not isinstance(output, dict):
        raise ValueError("diagnosis、thresholds、output 配置必须是映射")
    horizons = diagnosis.get("horizons", [1])
    if not horizons or any(int(value) < 1 for value in horizons):
        raise ValueError("diagnosis.horizons 必须是正整数列表")
    diagnosis["horizons"] = list(dict.fromkeys(int(value) for value in horizons))
    diagnosis["n_quantiles"] = int(diagnosis.get("n_quantiles", 10))
    if diagnosis["n_quantiles"] < 3:
        raise ValueError("diagnosis.n_quantiles 不能小于 3")
    return config


def load_factor_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    registry = load_yaml(path)
    factors = registry.get("factors")
    if not isinstance(factors, dict) or not factors:
        raise ValueError("factor_registry.yaml 必须包含非空 factors 映射")
    for name, metadata in factors.items():
        if not isinstance(metadata, dict):
            raise ValueError(f"因子 {name} 的元数据必须是映射")
        direction = int(metadata.get("direction", 1))
        if direction not in {-1, 1}:
            raise ValueError(f"因子 {name} 的 direction 必须为 1 或 -1")
        metadata["direction"] = direction
    return registry


def data_version(snapshot_root: str | Path) -> str:
    manifest = _resolve(snapshot_root) / "manifest.json"
    if not manifest.is_file():
        return "unknown"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_REGISTRY_PATH",
    "ROOT",
    "data_version",
    "deep_merge",
    "load_diagnosis_config",
    "load_factor_registry",
    "load_yaml",
]
