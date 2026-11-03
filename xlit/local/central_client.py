"""HTTP client the local service uses to talk to the Central Model Registry.

Designed for constrained / flaky courtroom links:

* short connect timeout, so an unreachable central fails fast and we stay offline-quiet;
* ETag polling, so "nothing changed" costs a 304 with no body;
* resumable downloads via HTTP Range into a ``.part`` file, verified by sha256
  before the file is atomically moved into place.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Callable

import requests

from xlit.common.logging_setup import get_logger, log_event
from xlit.common.manifest import FileEntry, Manifest, update_hash_from_stream

log = get_logger("local.central_client")

ProgressFn = Callable[[int], None]


class CentralUnavailable(RuntimeError):
    """Central could not be reached or returned a server error."""


class IntegrityError(RuntimeError):
    """Downloaded bytes do not match the manifest."""


class CentralClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        connect_timeout_s: float = 2.0,
        read_timeout_s: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.timeout = (connect_timeout_s, read_timeout_s)
        self.session = session or requests.Session()
        self.session.headers.update({"X-Client-ID": client_id, "User-Agent": "xlit-local/0.1"})

    def latest_manifest(self, lang: str, etag: str | None = None) -> tuple[Manifest | None, str | None]:
        """Return (manifest, etag). ``manifest`` is None when unchanged (304)."""
        headers = {"If-None-Match": etag} if etag else {}
        resp = self._get(f"/v1/models/{lang}/latest", headers=headers)
        if resp.status_code == 304:
            return None, etag
        if resp.status_code == 404:
            raise CentralUnavailable(f"no published model for {lang}")
        resp.raise_for_status()
        return Manifest.from_dict(resp.json()), resp.headers.get("ETag")

    def heartbeat(self, payload: dict) -> dict:
        try:
            resp = self.session.post(f"{self.base_url}/v1/heartbeat", json=payload, timeout=self.timeout)
            return resp.json() if resp.ok else {}
        except (requests.RequestException, ValueError):
            return {}

    def download_file(
        self,
        lang: str,
        version: str,
        entry: FileEntry,
        dest: Path,
        progress: ProgressFn | None = None,
        chunk: int = 1 << 20,
    ) -> None:
        """Download ``entry`` to ``dest`` (resuming any ``dest.part``), verify, then rename."""
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)

        h = hashlib.sha256()
        have = 0
        if part.exists():
            if part.stat().st_size > entry.size:
                part.unlink()
            else:
                with part.open("rb") as f:
                    have = update_hash_from_stream(h, f)
        if progress and have:
            progress(have)

        if have < entry.size:
            headers = {"Range": f"bytes={have}-"} if have else {}
            t0 = time.perf_counter()
            resp = self._get(
                f"/v1/models/{lang}/{version}/files/{entry.path}", headers=headers, stream=True
            )
            if resp.status_code == 200 and have:
                # Server ignored Range; restart from zero.
                h, have = hashlib.sha256(), 0
                mode = "wb"
            elif resp.status_code in (200, 206):
                mode = "ab" if have else "wb"
            else:
                resp.close()
                raise CentralUnavailable(f"download {entry.path}: HTTP {resp.status_code}")

            resumed_from = have
            try:
                with part.open(mode) as f:
                    for buf in resp.iter_content(chunk_size=chunk):
                        if not buf:
                            continue
                        f.write(buf)
                        h.update(buf)
                        have += len(buf)
                        if progress:
                            progress(len(buf))
            except requests.RequestException as e:
                raise CentralUnavailable(f"download {entry.path} interrupted at {have} bytes: {e}") from e
            finally:
                resp.close()
            log_event(
                log,
                logging.INFO,
                "file_downloaded",
                path=entry.path,
                bytes=have - resumed_from,
                resumed_from=resumed_from,
                duration_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        if have != entry.size or h.hexdigest() != entry.sha256:
            part.unlink(missing_ok=True)
            raise IntegrityError(f"{entry.path}: size/sha256 mismatch (got {have} bytes)")
        os.replace(part, dest)

    def _get(self, path: str, headers: dict | None = None, stream: bool = False) -> requests.Response:
        try:
            resp = self.session.get(
                f"{self.base_url}{path}", headers=headers or {}, timeout=self.timeout, stream=stream
            )
        except requests.RequestException as e:
            raise CentralUnavailable(str(e)) from e
        if resp.status_code >= 500:
            resp.close()
            raise CentralUnavailable(f"HTTP {resp.status_code} from central for {path}")
        return resp
