"""On-disk model registry used by the central server.

Layout::

    <root>/<lang>/LATEST                 -> "hi-1.0.0"
    <root>/<lang>/<version>/manifest.json
    <root>/<lang>/<version>/<artifact paths from manifest>

Publishing is atomic: files are staged in a hidden temp dir, the manifest is
written last, the dir is renamed into place, then ``LATEST`` is swapped with
``os.replace``. Readers therefore never observe a half-published version.
In production the same layout maps 1:1 onto an object-store bucket behind a CDN.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Iterable

from xlit.common.logging_setup import get_logger, log_event
from xlit.common.manifest import (
    FileEntry,
    Manifest,
    ManifestError,
    Package,
    sha256_file,
    validate_lang,
    validate_relpath,
    validate_version,
)

log = get_logger("central.registry")


class RegistryError(RuntimeError):
    pass


class ModelRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_cache: dict[tuple[str, str], tuple[float, Manifest]] = {}

    def latest_version(self, lang: str) -> str | None:
        pointer = self.root / validate_lang(lang) / "LATEST"
        if not pointer.is_file():
            return None
        return validate_version(pointer.read_text().strip())

    def list_versions(self, lang: str) -> list[str]:
        lang_dir = self.root / validate_lang(lang)
        if not lang_dir.is_dir():
            return []
        return sorted(
            p.name for p in lang_dir.iterdir() if p.is_dir() and (p / "manifest.json").is_file()
        )

    def get_manifest(self, lang: str, version: str) -> Manifest | None:
        path = self.root / validate_lang(lang) / validate_version(version) / "manifest.json"
        if not path.is_file():
            return None
        mtime = path.stat().st_mtime
        cached = self._manifest_cache.get((lang, version))
        if cached and cached[0] == mtime:
            return cached[1]
        manifest = Manifest.from_dict(json.loads(path.read_text()))
        self._manifest_cache[(lang, version)] = (mtime, manifest)
        return manifest

    def artifact_path(self, lang: str, version: str, relpath: str) -> tuple[Path, FileEntry]:
        """Resolve a downloadable file. Only paths listed in the manifest are served."""
        manifest = self.get_manifest(lang, version)
        if manifest is None:
            raise FileNotFoundError(f"{lang}/{version}")
        entry = manifest.entry(validate_relpath(relpath))
        if entry is None:
            raise FileNotFoundError(relpath)
        path = self.root / lang / version / relpath
        if not path.is_file():
            raise FileNotFoundError(relpath)
        return path, entry

    def set_latest(self, lang: str, version: str) -> None:
        if self.get_manifest(lang, version) is None:
            raise RegistryError(f"cannot point LATEST at unknown version {lang}/{version}")
        lang_dir = self.root / lang
        tmp = lang_dir / ".LATEST.tmp"
        tmp.write_text(version + "\n")
        os.replace(tmp, lang_dir / "LATEST")
        log_event(log, logging.INFO, "latest_updated", lang=lang, version=version)

    def publish(
        self,
        lang: str,
        version: str,
        packages: dict[str, Iterable[tuple[Path, str]]],
        engine: dict | None = None,
        link: bool = True,
        make_latest: bool = True,
    ) -> Manifest:
        """Publish a version.

        ``packages`` maps package name -> iterable of (source_file, artifact_relpath).
        With ``link=True`` files are hard-linked when possible (instant, no extra disk).
        """
        validate_lang(lang)
        validate_version(version)
        final_dir = self.root / lang / version
        if final_dir.exists():
            raise RegistryError(f"version already published: {lang}/{version} (versions are immutable)")

        staging = self.root / lang / f".{version}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        t0 = time.perf_counter()
        try:
            built: dict[str, Package] = {}
            for name, files in packages.items():
                entries = []
                for src, rel in files:
                    validate_relpath(rel)
                    dest = staging / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    _link_or_copy(src, dest, link)
                    entries.append(FileEntry(path=rel, size=dest.stat().st_size, sha256=sha256_file(dest)))
                built[name] = Package(name=name, files=tuple(sorted(entries, key=lambda e: e.path)))

            manifest = Manifest(
                lang=lang,
                version=version,
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                packages=built,
                engine=engine or {},
            )
            (staging / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2) + "\n")
            os.replace(staging, final_dir)
        except (OSError, ManifestError):
            shutil.rmtree(staging, ignore_errors=True)
            raise

        log_event(
            log,
            logging.INFO,
            "version_published",
            lang=lang,
            version=version,
            packages={n: p.size for n, p in manifest.packages.items()},
            duration_ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        if make_latest:
            self.set_latest(lang, version)
        return manifest


def _link_or_copy(src: Path, dest: Path, link: bool) -> None:
    if link:
        try:
            os.link(src, dest)
            return
        except OSError:
            pass
    shutil.copy2(src, dest)
