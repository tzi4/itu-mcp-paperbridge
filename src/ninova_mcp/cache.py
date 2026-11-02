"""Simple in-process TTL cache for hot Ninova reads."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class CacheEntry(Generic[T]):
    value: T
    expires_at: float
    stored_at: float = 0.0


class TtlCache(Generic[T]):
    """Single-process cache with a shared TTL per key."""

    def __init__(self, default_ttl_seconds: float) -> None:
        self.default_ttl_seconds = max(0.0, float(default_ttl_seconds))
        self._entries: dict[str, CacheEntry[T]] = {}

    def get(self, key: str) -> T | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry.value

    def age_seconds(self, key: str) -> float | None:
        """Return the age of a live entry without changing its expiry."""
        entry = self._entries.get(key)
        now = time.monotonic()
        if entry is None or entry.expires_at <= now:
            return None
        return max(0.0, now - entry.stored_at)

    def set(self, key: str, value: T, *, ttl_seconds: float | None = None) -> T:
        ttl = self.default_ttl_seconds if ttl_seconds is None else max(0.0, float(ttl_seconds))
        if ttl <= 0:
            return value
        now = time.monotonic()
        self._entries[key] = CacheEntry(value=value, expires_at=now + ttl, stored_at=now)
        return value

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._entries.clear()
            return
        self._entries.pop(key, None)

    def clear(self) -> None:
        self._entries.clear()


def parse_ttl_seconds(env_value: str | None, default: float) -> float:
    if env_value is None or not str(env_value).strip():
        return default
    try:
        return max(0.0, float(env_value))
    except ValueError:
        return default
