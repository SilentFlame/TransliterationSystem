"""Background reconcile loop: keeps the local engine at the best (version, tier) available.

Each cycle:
    1. Ask central for the latest manifest (ETag; 304 when unchanged).
       Unreachable -> mark offline and continue with what is installed locally.
    2. Ensure the ``base`` package is installed -> load/stage it if the policy says so.
    3. If rescoring is allowed, ensure the ``rescore`` package -> load/stage it.
    4. Prune versions that are neither active, staged, nor the target.
    5. Heartbeat to central (best effort).

The hot path (``/v1/suggest``) never waits on any of this.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

from xlit.common.logging_setup import get_logger, log_event
from xlit.common.manifest import PACKAGE_BASE, PACKAGE_RESCORE, FileEntry, Manifest
from xlit.local.central_client import CentralClient, CentralUnavailable, IntegrityError
from xlit.local.config import LocalConfig
from xlit.local.engine import LoadedEngine, LoadRequest, load_engine
from xlit.local.engine_manager import EngineManager, plan_next_load
from xlit.local.package_store import PackageStore

log = get_logger("local.sync_agent")

Loader = Callable[[LoadRequest], LoadedEngine]


class SyncAgent(threading.Thread):
    def __init__(
        self,
        cfg: LocalConfig,
        store: PackageStore,
        client: CentralClient,
        manager: EngineManager,
        loader: Loader = load_engine,
        started_at: float | None = None,
    ) -> None:
        super().__init__(name="sync-agent", daemon=True)
        self.cfg = cfg
        self.store = store
        self.client = client
        self.manager = manager
        self.loader = loader
        self.started_at = started_at or time.time()
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()
        self._manifest: Manifest | None = None
        self._etag: str | None = None
        self._lock = threading.Lock()
        self.central_state = "unknown"
        self.last_error: str | None = None
        self.last_sync_at: float | None = None
        self.loading: dict | None = None
        self.download: dict | None = None

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake_evt.set()

    def poke(self) -> None:
        """Run a reconcile cycle now (e.g. operator hit 'check for updates')."""
        self._wake_evt.set()

    def status(self) -> dict:
        with self._lock:
            return {
                "central_url": self.client.base_url,
                "central_state": self.central_state,
                "last_error": self.last_error,
                "last_sync_at": self.last_sync_at,
                "target_version": self._manifest.version if self._manifest else None,
                "rescore_mode": self.cfg.rescore_mode,
                "rescore_allowed": self.cfg.rescore_allowed(),
                "loading": self.loading,
                "download": self.download,
            }

    def run(self) -> None:
        self._boot_from_local()
        delay = self.cfg.poll_interval_s
        while not self._stop_evt.is_set():
            try:
                self.reconcile_once()
                delay = self.cfg.poll_interval_s
            except (CentralUnavailable, IntegrityError) as e:
                self._set_offline(e)
                delay = min(max(delay, 1.0) * 2, self.cfg.max_backoff_s)
            except Exception as e:  # keep the agent alive; the hot path must not depend on it
                log.exception("sync_cycle_crashed")
                self._set_offline(e)
                delay = min(max(delay, 1.0) * 2, self.cfg.max_backoff_s)
            if self.manager.active is None:
                # Nothing to serve yet: retry eagerly instead of backing off for minutes.
                delay = min(delay, self.cfg.bootstrap_retry_s)
            self._heartbeat()
            jittered = delay * random.uniform(0.8, 1.2)
            self._wake_evt.wait(jittered)
            self._wake_evt.clear()

    def _boot_from_local(self) -> None:
        """Serve from disk before touching the network: offline courts still get suggestions."""
        installed = self.store.installed_manifests(self.cfg.lang)
        if not installed:
            log_event(log, logging.INFO, "no_local_package", action="bootstrap_from_central")
            return
        m = installed[0]
        self._manifest = m
        self._load(m, PACKAGE_BASE)
        log_event(
            log,
            logging.INFO,
            "time_to_first_suggestion",
            ms=round((time.time() - self.started_at) * 1000, 1),
            source="local_cache",
            version=m.version,
        )

    def reconcile_once(self) -> None:
        online_error: Exception | None = None
        try:
            fetched, etag = self.client.latest_manifest(self.cfg.lang, self._etag)
            with self._lock:
                if self.central_state != "online":
                    log_event(log, logging.INFO, "central_online", url=self.client.base_url)
                self.central_state = "online"
                self.last_error = None
                self.last_sync_at = time.time()
            if fetched is not None:
                if self._manifest is None or fetched.version != self._manifest.version:
                    log_event(log, logging.INFO, "manifest_update", version=fetched.version,
                              previous=self._manifest.version if self._manifest else None)
                self._manifest, self._etag = fetched, etag
        except CentralUnavailable as e:
            online_error = e
            self._set_offline(e)

        m = self._manifest
        if m is None:
            installed = self.store.installed_manifests(self.cfg.lang)
            m = self._manifest = installed[0] if installed else None
        if m is None:
            if online_error:
                raise online_error
            return

        online = online_error is None
        had_engine = self.manager.active is not None
        if not self.store.is_ready(m, PACKAGE_BASE) and online:
            self._install(m, PACKAGE_BASE)
        self._maybe_load(m)
        if not had_engine and self.manager.active is not None:
            log_event(log, logging.INFO, "time_to_first_suggestion",
                      ms=round((time.time() - self.started_at) * 1000, 1), source="central_bootstrap",
                      version=m.version)

        if self.cfg.rescore_allowed() and m.has_rescore:
            if not self.store.is_ready(m, PACKAGE_RESCORE) and online:
                self._install(m, PACKAGE_RESCORE)
            self._maybe_load(m)

        keep = {m.version}
        for e in (self.manager.active, self.manager.staged):
            if e is not None:
                keep.add(e.version)
        self.store.prune(self.cfg.lang, keep)

        if online_error:
            raise online_error

    def _maybe_load(self, m: Manifest) -> None:
        active = self.manager.active.key if self.manager.active else None
        staged = self.manager.staged.key if self.manager.staged else None
        nxt = plan_next_load(
            active=active,
            staged=staged,
            target_version=m.version,
            base_ready=self.store.is_ready(m, PACKAGE_BASE),
            rescore_ready=self.store.is_ready(m, PACKAGE_RESCORE),
            rescore_allowed=self.cfg.rescore_allowed(),
            target_has_rescore=m.has_rescore,
        )
        if nxt and self.manager.wants(*nxt):
            self._load(m, nxt[1])

    def _load(self, m: Manifest, tier: str) -> None:
        req = LoadRequest(
            lang=m.lang,
            version=m.version,
            tier=tier,
            models_path=self.store.models_path(m.lang, m.version),
            beam_width=self.cfg.beam_width,
        )
        with self._lock:
            self.loading = {"version": m.version, "tier": tier, "since": time.time()}
        try:
            self.manager.offer(self.loader(req))
        except Exception:
            log.exception("engine_load_failed", extra={"fields": {"version": m.version, "tier": tier}})
        finally:
            with self._lock:
                self.loading = None

    def _install(self, m: Manifest, package: str) -> None:
        total = m.packages[package].size
        done = total - self.store.missing_bytes(m, package)
        with self._lock:
            self.download = {"version": m.version, "package": package, "done": done, "total": total}
        log_event(log, logging.INFO, "package_download_started", version=m.version, package=package,
                  total_bytes=total, already_have=done)
        t0 = time.perf_counter()

        def progress(n: int) -> None:
            with self._lock:
                if self.download:
                    self.download["done"] += n

        def fetch(manifest: Manifest, entry: FileEntry, dest) -> None:
            self.client.download_file(manifest.lang, manifest.version, entry, dest, progress=progress)

        try:
            self.store.install(m, package, fetch)
        finally:
            with self._lock:
                self.download = None
        log_event(log, logging.INFO, "package_download_finished", version=m.version, package=package,
                  duration_ms=round((time.perf_counter() - t0) * 1000, 1))

    def _set_offline(self, e: Exception) -> None:
        with self._lock:
            changed = self.central_state != "offline"
            self.central_state = "offline"
            self.last_error = str(e)[:300]
        if changed:
            log_event(log, logging.WARNING, "central_offline", error=str(e)[:300],
                      serving=self.manager.active.describe() if self.manager.active else None)

    def _heartbeat(self) -> None:
        if self.central_state != "online":
            return
        st = self.manager.status()
        active = st["active"] or {}
        self.client.heartbeat({
            "client_id": self.client.client_id,
            "lang": self.cfg.lang,
            "active_version": active.get("version"),
            "active_tier": active.get("tier"),
            "staged": st["staged"],
            "central_state": self.central_state,
            "rescore_mode": self.cfg.rescore_mode,
            "uptime_s": round(time.time() - self.started_at),
            "lookups": st["lookups"],
            "cache_hit_rate": st["cache"]["hit_rate"],
            "p95_compute_ms": st["p95_compute_ms"],
        })
