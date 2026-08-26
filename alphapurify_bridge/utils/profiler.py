"""Low-overhead timing helpers and JSON-lines performance logging."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from alphapurify_bridge.config import ROOT
from alphapurify_bridge.io import json_safe


PERF_STAGE_NAMES = (
    "data_load",
    "data_prep",
    "factor_extract",
    "alphapurify",
    "metrics",
    "report",
    "serialize",
)


def merge_stages(*values: Mapping[str, float] | None) -> dict[str, float]:
    """Return a complete, additive stage mapping in the canonical order."""

    merged = {name: 0.0 for name in PERF_STAGE_NAMES}
    for value in values:
        if not value:
            continue
        for name, duration in value.items():
            if name in merged:
                merged[name] += max(0.0, float(duration))
    return merged


class StageTimer:
    """Accumulate wall-clock durations for named diagnosis stages."""

    def __init__(self, enabled: bool = True):
        self.enabled = bool(enabled)
        self._stages = merge_stages()

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        if stage not in self._stages:
            raise ValueError(f"未知性能阶段：{stage}")
        if not self.enabled:
            yield
            return
        started = time.perf_counter()
        try:
            yield
        finally:
            self._stages[stage] += time.perf_counter() - started

    def add(self, stage: str, duration: float) -> None:
        if stage not in self._stages:
            raise ValueError(f"未知性能阶段：{stage}")
        if self.enabled:
            self._stages[stage] += max(0.0, float(duration))

    @property
    def stages(self) -> dict[str, float]:
        return dict(self._stages)


class PerformanceLog:
    """Append factor and batch performance records as UTF-8 JSON lines."""

    def __init__(self, path: str | Path = "logs/alphapurify_perf.log"):
        target = Path(path)
        self.path = target if target.is_absolute() else ROOT / target
        self._lock = threading.Lock()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().astimezone().replace(microsecond=0).isoformat()

    def append(self, payload: Mapping[str, Any]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(json_safe(dict(payload)), ensure_ascii=False, allow_nan=False)
        with self._lock, self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return self.path

    def factor(
        self,
        factor_name: str,
        *,
        stages: Mapping[str, float],
        total: float,
        rows: int,
        symbols: int | None = None,
        dates: int | None = None,
        run: int | None = None,
        mode: str = "full",
    ) -> Path:
        payload: dict[str, Any] = {
            "timestamp": self._timestamp(),
            "type": "factor",
            "factor": str(factor_name),
            "mode": mode,
            "stages": merge_stages(stages),
            "total": max(0.0, float(total)),
            "rows": int(rows),
            "symbols": None if symbols is None else int(symbols),
            "dates": None if dates is None else int(dates),
        }
        if run is not None:
            payload["run"] = int(run)
        return self.append(payload)

    def batch(
        self,
        factors: Sequence[str],
        *,
        stages: Mapping[str, float],
        total: float,
        per_factor: Mapping[str, float],
        run: int | None = None,
        mode: str = "full",
    ) -> Path:
        payload: dict[str, Any] = {
            "timestamp": self._timestamp(),
            "type": "batch",
            "mode": mode,
            "factors": [str(value) for value in factors],
            "total_duration": max(0.0, float(total)),
            "stage_breakdown": merge_stages(stages),
            "per_factor": {str(key): max(0.0, float(value)) for key, value in per_factor.items()},
        }
        if run is not None:
            payload["run"] = int(run)
        return self.append(payload)


__all__ = ["PERF_STAGE_NAMES", "PerformanceLog", "StageTimer", "merge_stages"]
