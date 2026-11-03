"""Engine slots, prefix cache, and the word-boundary promotion policy.

State per process::

    active  - serves every lookup. None only before the first package is installed.
    staged  - fully loaded + warmed replacement (rescore upgrade or new version),
              waiting for a *word boundary* so the ranked list never reshuffles
              under the typist's fingers mid-word.

Promotion triggers (any of):
    * the next lookup is the first keystroke of a new word (``word_start``)
    * the client reports a commit (space / punctuation / pick)
    * the typist has been idle for ``idle_promote_s``
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from xlit.common.logging_setup import get_logger, log_event
from xlit.local.engine import TIER_RANK, TIER_RESCORE, LoadedEngine

log = get_logger("local.engine_manager")


class EngineNotReady(RuntimeError):
    """No model package has been installed yet (fresh machine and central unreachable)."""


@dataclass(frozen=True)
class Suggestion:
    candidates: list[str]
    version: str
    tier: str
    cached: bool
    compute_ms: float


class LRUCache:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self.hits += 1
                return self._data[key]
            self.misses += 1
            return None

    def put(self, key, value) -> None:
        if self.capacity <= 0:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0


class EngineManager:
    def __init__(self, cache_size: int = 4096, idle_promote_s: float = 2.0, latency_window: int = 1000) -> None:
        self._lock = threading.RLock()
        self._active: LoadedEngine | None = None
        self._staged: LoadedEngine | None = None
        self._last_activity = time.monotonic()
        self._idle_promote_s = idle_promote_s
        self._cache = LRUCache(cache_size)
        self._compute_ms: deque[float] = deque(maxlen=latency_window)
        self.lookups = 0
        self.promotions: list[dict] = []
        self.first_ready_at: float | None = None

    @property
    def active(self) -> LoadedEngine | None:
        return self._active

    @property
    def staged(self) -> LoadedEngine | None:
        return self._staged

    def offer(self, engine: LoadedEngine) -> None:
        """Hand over a freshly loaded engine. Becomes active immediately if nothing is serving."""
        with self._lock:
            if self._active is None:
                self._activate(engine, reason="first_engine")
                return
            old_staged = self._staged
            self._staged = engine
            log_event(
                log,
                logging.INFO,
                "engine_staged",
                staged=engine.describe(),
                active=self._active.describe(),
                waiting_for="word_boundary_or_idle",
            )
        if old_staged is not None:
            del old_staged
            gc.collect()

    def suggest(self, prefix: str, topk: int, word_start: bool = False) -> Suggestion:
        now = time.monotonic()
        with self._lock:
            if self._staged is not None:
                if word_start:
                    self._promote("word_start")
                elif now - self._last_activity >= self._idle_promote_s:
                    self._promote("idle")
            self._last_activity = now
            engine = self._active
        if engine is None:
            raise EngineNotReady("no model installed yet")

        self.lookups += 1
        key = (engine.generation, prefix, topk)
        hit = self._cache.get(key)
        if hit is not None:
            return Suggestion(hit, engine.version, engine.tier, True, 0.0)

        t0 = time.perf_counter()
        cands = engine.suggest(prefix, topk)
        ms = (time.perf_counter() - t0) * 1000
        self._compute_ms.append(ms)
        self._cache.put(key, cands)
        return Suggestion(cands, engine.version, engine.tier, False, ms)

    def word_boundary(self) -> bool:
        """Client committed a word. Returns True if a staged engine was promoted."""
        with self._lock:
            self._last_activity = time.monotonic()
            if self._staged is None:
                return False
            self._promote("commit")
            return True

    def maybe_promote_idle(self) -> bool:
        with self._lock:
            if self._staged is not None and time.monotonic() - self._last_activity >= self._idle_promote_s:
                self._promote("idle")
                return True
        return False

    def wants(self, version: str, tier: str) -> bool:
        """Should the sync agent bother loading (version, tier)?"""
        with self._lock:
            for e in (self._active, self._staged):
                if e is not None and e.key == (version, tier):
                    return False
            return True

    def p95_compute_ms(self) -> float | None:
        if not self._compute_ms:
            return None
        s = sorted(self._compute_ms)
        return round(s[min(len(s) - 1, int(0.95 * (len(s) - 1) + 0.5))], 2)

    def status(self) -> dict:
        self.maybe_promote_idle()
        with self._lock:
            return {
                "ready": self._active is not None,
                "active": self._active.describe() if self._active else None,
                "staged": self._staged.describe() if self._staged else None,
                "lookups": self.lookups,
                "cache": {"size": len(self._cache), "hit_rate": self._cache.hit_rate},
                "p95_compute_ms": self.p95_compute_ms(),
                "promotions": self.promotions[-5:],
            }

    def _promote(self, reason: str) -> None:
        staged, self._staged = self._staged, None
        if staged is not None:
            self._activate(staged, reason)

    def _activate(self, engine: LoadedEngine, reason: str) -> None:
        old = self._active
        self._active = engine
        self._cache.clear()
        if self.first_ready_at is None:
            self.first_ready_at = time.time()
        record = {
            "at": time.strftime("%H:%M:%S"),
            "reason": reason,
            "from": old.describe() if old else None,
            "to": engine.describe(),
        }
        self.promotions.append(record)
        log_event(log, logging.INFO, "engine_promoted", **record)
        if old is not None:
            # In-flight lookups keep their own reference; memory is reclaimed after they finish.
            del old
            gc.collect()


def plan_next_load(
    active: tuple[str, str] | None,
    staged: tuple[str, str] | None,
    target_version: str,
    base_ready: bool,
    rescore_ready: bool,
    rescore_allowed: bool,
    target_has_rescore: bool = True,
) -> tuple[str, str] | None:
    """Pure policy: which (version, tier) should the sync agent load next, if any?

    * Never block a boot on rescore: with nothing serving, load base first.
    * Never downgrade quality on an update: if we're already serving rescore and
      the new version ships one, wait for it rather than stepping down to base.
    """
    current = staged or active
    want_rescore = rescore_allowed and rescore_ready

    if current is None:
        return (target_version, "base") if base_ready else None

    cur_version, cur_tier = current
    if cur_version == target_version:
        if want_rescore and TIER_RANK[cur_tier] < TIER_RANK[TIER_RESCORE]:
            return (target_version, TIER_RESCORE)
        return None

    if want_rescore:
        return (target_version, TIER_RESCORE)
    if cur_tier == TIER_RESCORE and rescore_allowed and target_has_rescore:
        return None
    return (target_version, "base") if base_ready else None
