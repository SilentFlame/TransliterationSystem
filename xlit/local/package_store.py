"""Local, versioned model package cache on the typist machine.

Layout::

    <root>/client_id
    <root>/packages/<lang>/<version>/manifest.json
    <root>/packages/<lang>/<version>/.ready-base         <- package fully verified
    <root>/packages/<lang>/<version>/.ready-rescore
    <root>/packages/<lang>/<version>/lang_list.txt
    <root>/packages/<lang>/<version>/model/...           <- engine models_path

A package is "ready" only after every file passed sha256 verification and the
ready-marker was written. Boot uses markers (cheap) instead of rehashing GBs.
Identical files across versions are hard-linked locally instead of re-downloaded.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable

from xlit.common.logging_setup import get_logger, log_event
from xlit.common.manifest import FileEntry, Manifest, ManifestError, validate_lang, validate_version

log = get_logger("local.package_store")

Downloader = Callable[[Manifest, FileEntry, Path], None]


class PackageStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.packages_root = root / "packages"
        self.packages_root.mkdir(parents=True, exist_ok=True)

    def client_id(self) -> str:
        p = self.root / "client_id"
        if not p.is_file():
            p.write_text(uuid.uuid4().hex + "\n")
        return p.read_text().strip()

    def version_dir(self, lang: str, version: str) -> Path:
        return self.packages_root / validate_lang(lang) / validate_version(version)

    def models_path(self, lang: str, version: str) -> Path:
        return self.version_dir(lang, version) / "model"

    def save_manifest(self, manifest: Manifest) -> None:
        d = self.version_dir(manifest.lang, manifest.version)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / ".manifest.json.tmp"
        tmp.write_text(json.dumps(manifest.to_dict(), indent=2) + "\n")
        os.replace(tmp, d / "manifest.json")

    def load_manifest(self, lang: str, version: str) -> Manifest | None:
        p = self.version_dir(lang, version) / "manifest.json"
        if not p.is_file():
            return None
        try:
            return Manifest.from_dict(json.loads(p.read_text()))
        except (ValueError, ManifestError) as e:
            log_event(log, logging.WARNING, "corrupt_local_manifest", lang=lang, version=version, error=str(e))
            return None

    def installed_manifests(self, lang: str) -> list[Manifest]:
        """Manifests with at least the base package ready, newest first."""
        lang_dir = self.packages_root / validate_lang(lang)
        if not lang_dir.is_dir():
            return []
        out = []
        for d in lang_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            m = self.load_manifest(lang, d.name)
            if m and self.is_ready(m, "base"):
                out.append(m)
        return sorted(out, key=lambda m: (m.created_at, m.version), reverse=True)

    def is_ready(self, manifest: Manifest, package: str) -> bool:
        if package not in manifest.packages:
            return False
        d = self.version_dir(manifest.lang, manifest.version)
        if not (d / f".ready-{package}").is_file():
            return False
        return all(
            (d / f.path).is_file() and (d / f.path).stat().st_size == f.size
            for f in manifest.packages[package].files
        )

    def missing_bytes(self, manifest: Manifest, package: str) -> int:
        d = self.version_dir(manifest.lang, manifest.version)
        return sum(f.size for f in manifest.packages[package].files if not (d / f.path).is_file())

    def install(self, manifest: Manifest, package: str, download: Downloader) -> None:
        """Make ``package`` ready, downloading only what is missing."""
        self.save_manifest(manifest)
        d = self.version_dir(manifest.lang, manifest.version)
        for entry in manifest.packages[package].files:
            dest = d / entry.path
            if dest.is_file() and dest.stat().st_size == entry.size:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self._link_from_other_version(manifest, entry, dest):
                continue
            download(manifest, entry, dest)
        (d / f".ready-{package}").write_text("ok\n")
        log_event(log, logging.INFO, "package_ready", lang=manifest.lang, version=manifest.version, package=package)

    def prune(self, lang: str, keep: set[str]) -> list[str]:
        removed = []
        lang_dir = self.packages_root / validate_lang(lang)
        if not lang_dir.is_dir():
            return removed
        for d in lang_dir.iterdir():
            if d.is_dir() and d.name not in keep and not d.name.startswith("."):
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
        if removed:
            log_event(log, logging.INFO, "versions_pruned", lang=lang, removed=removed, kept=sorted(keep))
        return removed

    def _link_from_other_version(self, manifest: Manifest, entry: FileEntry, dest: Path) -> bool:
        """Reuse a byte-identical file (same sha256) already verified under another version."""
        for other in self.installed_manifests(manifest.lang):
            if other.version == manifest.version:
                continue
            match = other.entry(entry.path)
            if not match or match.sha256 != entry.sha256:
                continue
            src = self.version_dir(other.lang, other.version) / match.path
            if not src.is_file() or src.stat().st_size != entry.size:
                continue
            try:
                os.link(src, dest)
            except OSError:
                shutil.copy2(src, dest)
            log_event(log, logging.INFO, "file_reused_locally", path=entry.path, from_version=other.version)
            return True
        return False
