"""In-memory read-through cache with stale serving. One process, so no shared store needed."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gzetryn.clock import Clock


@dataclass(frozen=True)
class Entry:
    value: Any
    fetched_at: datetime
    stored: float  # monotonic


def cache_key(endpoint: str, path_params: dict | None, params: dict | None, body: dict | None) -> tuple:
    def norm(d: dict | None) -> tuple:
        return tuple(sorted((k, json.dumps(v, sort_keys=True, default=str)) for k, v in (d or {}).items() if v is not None))

    return (endpoint, norm(path_params), norm(params), json.dumps(body, sort_keys=True) if body else "")


class TtlCache:
    def __init__(self, max_entries: int = 5000, clock: Clock | None = None):
        self._max = max_entries
        self._clock = clock or Clock()
        self._data: OrderedDict[tuple, Entry] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def put(self, key: tuple, value: Any, fetched_at: datetime) -> None:
        self._data[key] = Entry(value, fetched_at, self._clock.monotonic())
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def get(self, key: tuple, max_age_sec: float) -> tuple[Entry, float] | None:
        e = self._data.get(key)
        if e is None:
            self.misses += 1
            return None
        age = self._clock.monotonic() - e.stored
        if age > max_age_sec:
            self.misses += 1
            return None
        self.hits += 1
        return e, age

    def get_stale(self, key: tuple) -> tuple[Entry, float] | None:
        e = self._data.get(key)
        return (e, self._clock.monotonic() - e.stored) if e else None

    def __len__(self) -> int:
        return len(self._data)
