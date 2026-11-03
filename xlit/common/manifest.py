"""Model package manifest: the contract between central registry and local service.

A *version* (e.g. ``hi-1.0.0``) contains one or more *packages*:

* ``base``    - transformer weights + vocab. Enough to serve suggestions.
* ``rescore`` - unigram word-probability dictionary. Optional quality upgrade.

Each package is a list of files with size and sha256, laid out relative to the
version directory exactly as the local engine expects to find them on disk.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

SCHEMA_VERSION = 1
PACKAGE_BASE = "base"
PACKAGE_RESCORE = "rescore"
KNOWN_PACKAGES = (PACKAGE_BASE, PACKAGE_RESCORE)

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """Manifest is malformed or references an unsafe path."""


def validate_version(version: str) -> str:
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ManifestError(f"invalid version: {version!r}")
    return version


def validate_lang(lang: str) -> str:
    if not isinstance(lang, str) or not _LANG_RE.match(lang):
        raise ManifestError(f"invalid lang: {lang!r}")
    return lang


def validate_relpath(path: str) -> str:
    """Reject absolute paths, traversal, and backslashes; artifacts are posix-relative."""
    if not isinstance(path, str) or not path or "\\" in path:
        raise ManifestError(f"invalid artifact path: {path!r}")
    if PurePosixPath(path).is_absolute() or any(part in ("..", ".", "") for part in path.split("/")):
        raise ManifestError(f"unsafe artifact path: {path!r}")
    return path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        update_hash_from_stream(h, f, chunk)
    return h.hexdigest()


def update_hash_from_stream(h: "hashlib._Hash", f: BinaryIO, chunk: int = 1 << 20) -> int:
    total = 0
    while True:
        buf = f.read(chunk)
        if not buf:
            return total
        h.update(buf)
        total += len(buf)


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FileEntry":
        path = validate_relpath(d.get("path", ""))
        size = d.get("size")
        sha = d.get("sha256", "")
        if not isinstance(size, int) or size < 0:
            raise ManifestError(f"invalid size for {path}")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise ManifestError(f"invalid sha256 for {path}")
        return cls(path=path, size=size, sha256=sha)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class Package:
    name: str
    files: tuple[FileEntry, ...]

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> "Package":
        if name not in KNOWN_PACKAGES:
            raise ManifestError(f"unknown package: {name!r}")
        files = d.get("files")
        if not isinstance(files, list) or not files:
            raise ManifestError(f"package {name} has no files")
        return cls(name=name, files=tuple(FileEntry.from_dict(f) for f in files))

    def to_dict(self) -> dict[str, Any]:
        return {"size": self.size, "files": [f.to_dict() for f in self.files]}


@dataclass(frozen=True)
class Manifest:
    lang: str
    version: str
    created_at: str
    packages: dict[str, Package]
    engine: dict[str, Any] = field(default_factory=dict)
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_lang(self.lang)
        validate_version(self.version)
        if PACKAGE_BASE not in self.packages:
            raise ManifestError("manifest must contain a base package")

    @property
    def has_rescore(self) -> bool:
        return PACKAGE_RESCORE in self.packages

    def entry(self, relpath: str) -> FileEntry | None:
        for pkg in self.packages.values():
            for f in pkg.files:
                if f.path == relpath:
                    return f
        return None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        if not isinstance(d, dict):
            raise ManifestError("manifest must be an object")
        if d.get("schema") != SCHEMA_VERSION:
            raise ManifestError(f"unsupported manifest schema: {d.get('schema')!r}")
        pkgs_raw = d.get("packages")
        if not isinstance(pkgs_raw, dict):
            raise ManifestError("manifest.packages must be an object")
        return cls(
            lang=d.get("lang", ""),
            version=d.get("version", ""),
            created_at=str(d.get("created_at", "")),
            packages={name: Package.from_dict(name, p) for name, p in pkgs_raw.items()},
            engine=dict(d.get("engine") or {}),
            schema=SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "lang": self.lang,
            "version": self.version,
            "created_at": self.created_at,
            "engine": self.engine,
            "packages": {name: p.to_dict() for name, p in self.packages.items()},
        }
