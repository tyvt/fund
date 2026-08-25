"""Common strategy contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any, Mapping


class BaseStrategy(ABC):
    def __init__(self, params: Mapping[str, Any] | None = None):
        self.params = dict(params or {})

    @abstractmethod
    def generate_signals(self, data):
        """Return ``(target_weights, metadata)``."""

    def with_params(self, overrides: Mapping[str, Any] | None = None):
        params = deepcopy(self.params)
        params.update(dict(overrides or {}))
        return type(self)(params)

    def rebalance_frequency(self) -> str:
        return str(self.params.get("rebalance_freq", "A")).upper()

    def rebalance_month(self) -> int:
        return int(self.params.get("rebalance_month", 1))
