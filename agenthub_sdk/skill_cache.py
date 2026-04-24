"""L1 SDK Auto Cache — caches skill execution results for injection into run context."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from webaa_sdk.models import SkillCachePolicy


class Freshness(Enum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


@dataclass
class _CacheEntry:
    result: Dict[str, Any]
    freshness: Freshness
    timestamp: float  # time.time()
    policy: SkillCachePolicy


class SkillCache:
    """Thread-safe L1 skill result cache."""

    def __init__(self) -> None:
        self._cache: Dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    def put(
        self,
        skill_name: str,
        result: Dict[str, Any],
        policy: Optional[SkillCachePolicy],
    ) -> None:
        """Cache a skill result according to its cache policy."""
        if policy is None or not policy.enabled:
            return

        with self._lock:
            if policy.mode == "snapshot":
                self._cache[skill_name] = _CacheEntry(
                    result=result,
                    freshness=Freshness.FRESH,
                    timestamp=time.time(),
                    policy=policy,
                )
            elif policy.mode == "append":
                existing = self._cache.get(skill_name)
                if existing is not None:
                    merged = {**existing.result, **result}
                    self._cache[skill_name] = _CacheEntry(
                        result=merged,
                        freshness=Freshness.FRESH,
                        timestamp=time.time(),
                        policy=policy,
                    )
                else:
                    self._cache[skill_name] = _CacheEntry(
                        result=result,
                        freshness=Freshness.FRESH,
                        timestamp=time.time(),
                        policy=policy,
                    )
            # mode == "none": don't cache

    def build_context(self) -> Optional[Dict[str, Any]]:
        """Build the skill_cache context map. Only includes fresh/stale entries."""
        self._refresh_freshness()
        ctx: Dict[str, Any] = {}
        with self._lock:
            for name, entry in self._cache.items():
                if entry.freshness == Freshness.EXPIRED:
                    continue
                ctx[name] = {
                    "result": entry.result,
                    "freshness": entry.freshness.value,
                }
        return ctx if ctx else None

    def invalidate(self, event_name: str) -> None:
        """Invalidate cache entries matching the given event name."""
        with self._lock:
            for entry in self._cache.values():
                if entry.freshness == Freshness.EXPIRED:
                    continue
                if event_name in entry.policy.invalidate_on:
                    entry.freshness = Freshness.STALE

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()

    def _refresh_freshness(self) -> None:
        now = time.time()
        with self._lock:
            for entry in self._cache.values():
                if entry.freshness == Freshness.EXPIRED:
                    continue
                ttl_sec = entry.policy.ttl / 1000.0  # ttl is in ms
                if ttl_sec > 0 and (now - entry.timestamp) > ttl_sec:
                    entry.freshness = Freshness.EXPIRED
