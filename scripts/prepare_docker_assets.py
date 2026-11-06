#!/usr/bin/env python3
"""Stage model bytes for the Docker images from an already-published registry.

Writes two trees (both gitignored, regenerated on every ``make docker-build``):

    docker/preloaded_registry/<lang>/LATEST
    docker/preloaded_registry/<lang>/<version>/{manifest.json,model/...}
        -> baked into the ``central`` image

    docker/preloaded_store/client_id
    docker/preloaded_store/packages/<lang>/<version>/{manifest.json,.ready-*,model/...}
        -> baked into the ``offline`` image so it boots without central

By default only the base package (~138 MB) is staged. ``--with-rescore`` keeps
the rescore package (~358 MB more, ~1.7 GB RSS when loaded) in both trees.
Files are hard-linked when possible so staging costs no extra disk.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xlit.common.manifest import PACKAGE_BASE, PACKAGE_RESCORE, Manifest  # noqa: E402


def _newest_version(registry: Path, lang: str) -> Path | None:
    lang_dir = registry / lang
    latest = lang_dir / "LATEST"
    if latest.is_file():
        d = lang_dir / latest.read_text().strip()
        if (d / "manifest.json").is_file():
            return d
    if not lang_dir.is_dir():
        return None
    candidates = [d for d in lang_dir.iterdir()
                  if d.is_dir() and not d.name.startswith(".") and (d / "manifest.json").is_file()]
    return sorted(candidates, key=lambda d: d.name)[-1] if candidates else None


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _stage(source: Path, manifest: Manifest, version_dir: Path, packages: list[str]) -> int:
    version_dir.mkdir(parents=True)
    slim = Manifest(
        lang=manifest.lang,
        version=manifest.version,
        created_at=manifest.created_at,
        packages={p: manifest.packages[p] for p in packages},
        engine=dict(manifest.engine),
    )
    (version_dir / "manifest.json").write_text(json.dumps(slim.to_dict(), indent=2) + "\n")
    total = 0
    for p in packages:
        for entry in manifest.packages[p].files:
            src = source / entry.path
            if not src.is_file():
                raise FileNotFoundError(src)
            _link_or_copy(src, version_dir / entry.path)
            total += entry.size
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", type=Path, default=ROOT / "var" / "registry",
                    help="Published registry to stage from (default: var/registry)")
    ap.add_argument("--lang", default="hi")
    ap.add_argument("--out", type=Path, default=ROOT / "docker")
    ap.add_argument("--with-rescore", action="store_true", help="Also stage the rescore package")
    args = ap.parse_args()

    source = _newest_version(args.registry, args.lang)
    if source is None:
        print(f"No published version under {args.registry}/{args.lang}. "
              "Run: make setup && make warm-model && make publish", file=sys.stderr)
        return 1

    manifest = Manifest.from_dict(json.loads((source / "manifest.json").read_text()))
    packages = [PACKAGE_BASE]
    if args.with_rescore:
        if PACKAGE_RESCORE not in manifest.packages:
            print(f"{manifest.version} has no rescore package", file=sys.stderr)
            return 1
        packages.append(PACKAGE_RESCORE)

    registry_root = args.out / "preloaded_registry"
    store_root = args.out / "preloaded_store"
    for d in (registry_root, store_root):
        if d.exists():
            shutil.rmtree(d)

    try:
        total = _stage(source, manifest, registry_root / manifest.lang / manifest.version, packages)
        (registry_root / manifest.lang / "LATEST").write_text(manifest.version + "\n")

        store_version = store_root / "packages" / manifest.lang / manifest.version
        _stage(source, manifest, store_version, packages)
    except FileNotFoundError as e:
        print(f"missing file in source registry: {e}", file=sys.stderr)
        return 1
    for p in packages:
        (store_version / f".ready-{p}").write_text("ok\n")
    (store_root / "client_id").write_text("docker-offline\n")

    print(f"staged {manifest.lang}/{manifest.version} [{', '.join(packages)}] "
          f"{total / 1e6:.1f} MB from {source}")
    print(f"  registry -> {registry_root}")
    print(f"  store    -> {store_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
