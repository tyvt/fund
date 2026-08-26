"""Small thread-safe in-memory caches for factor diagnosis data."""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Generic, Hashable, Iterator, TypeVar


T = TypeVar("T")


class FactorDataCache(Generic[T]):
    """Bounded LRU cache keyed by factor/date/configuration tuples.

    The cache is deliberately process-local: it accelerates notebook sessions and
    repeated acceptance runs without writing derived data into the read-only lake.
    """

    def __init__(self, max_entries: int = 64):
        if int(max_entries) < 1:
            raise ValueError("max_entries 必须大于 0")
        self.max_entries = int(max_entries)
        self._values: OrderedDict[Hashable, T] = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0

    def get(self, key: Hashable, default: T | None = None) -> T | None:
        with self._lock:
            if key not in self._values:
                self.misses += 1
                return default
            self.hits += 1
            self._values.move_to_end(key)
            return self._values[key]

    def set(self, key: Hashable, value: T) -> None:
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > self.max_entries:
                self._values.popitem(last=False)

    def get_factor_data(
        self,
        factor_name: str,
        start_date: str | None,
        end_date: str | None,
        *configuration: Hashable,
    ) -> T | None:
        return self.get((str(factor_name), start_date, end_date, *configuration))

    def set_factor_data(
        self,
        factor_name: str,
        start_date: str | None,
        end_date: str | None,
        value: T,
        *configuration: Hashable,
    ) -> None:
        self.set((str(factor_name), start_date, end_date, *configuration), value)

    def items(self) -> Iterator[tuple[Hashable, T]]:
        with self._lock:
            return iter(list(self._values.items()))

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self.hits = 0
            self.misses = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)


__all__ = ["FactorDataCache"]
