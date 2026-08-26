"""Configurable PASS/WARNING/FAIL rules for factor diagnostics."""

from __future__ import annotations

import math
from typing import Any, Mapping


class ThresholdFilter:
    """Evaluate mandatory strength checks and advisory IC-decay checks."""

    def __init__(self, config: Mapping[str, Any]):
        self.config = dict(config)

    @staticmethod
    def _finite(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    def _minimum(self, value: Any, threshold: float) -> str:
        return "PASS" if self._finite(value) and float(value) >= float(threshold) else "FAIL"

    def evaluate(self, result: Mapping[str, Any]) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        mandatory = {
            "ic_mean": (result.get("ic_mean"), float(self.config.get("ic_mean_min", 0.015))),
            "ic_ir": (result.get("ic_ir"), float(self.config.get("ic_ir_min", 0.3))),
            "spread_return": (
                result.get("spread_return"),
                float(self.config.get("spread_return_min", 0.02)),
            ),
        }
        for name, (value, threshold) in mandatory.items():
            checks[name] = {
                "value": value,
                "threshold": threshold,
                "operator": ">=",
                "status": self._minimum(value, threshold),
            }
        if bool(self.config.get("quantile_monotonicity", True)):
            value = bool(result.get("quantile_monotonicity", False))
            checks["quantile_monotonicity"] = {
                "value": value,
                "threshold": True,
                "operator": "is",
                "status": "PASS" if value else "FAIL",
            }
        decay_values = [
            float(value)
            for key, value in (result.get("ic_decay") or {}).items()
            if key != "horizon_1" and self._finite(value)
        ]
        decay = max(decay_values) if decay_values else 0.0
        decay_threshold = float(
            self.config.get("max_ic_decay", self.config.get("ic_decay_max", 0.5))
        )
        checks["ic_decay"] = {
            "value": decay,
            "threshold": decay_threshold,
            "operator": "<=",
            "status": "PASS" if decay <= decay_threshold else "WARNING",
        }
        failures = [name for name, check in checks.items() if check["status"] == "FAIL"]
        warnings = [name for name, check in checks.items() if check["status"] == "WARNING"]
        status = "FAIL" if failures else "PASS"
        if failures:
            summary = "未通过：" + "、".join(failures)
        elif warnings:
            summary = "通过（警告：" + "、".join(warnings) + "）"
        else:
            summary = "所有检查通过"
        return {"status": status, "checks": checks, "warnings": warnings, "summary": summary}


__all__ = ["ThresholdFilter"]
