"""Publish IndicXlit weights into the central registry.

Usage::

    python -m xlit.central.publish --version hi-1.0.0            # base + rescore
    python -m xlit.central.publish --version hi-1.0.0 --no-rescore

By default the source is the model directory that ``ai4bharat-transliteration``
downloaded into its own install location (``.../models/en2indic/v1.0``). Run
``python -c "from ai4bharat.transliteration import XlitEngine; XlitEngine('hi', rescore=True)"``
once beforehand if that directory does not exist yet.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
from pathlib import Path

from xlit.common.logging_setup import configure_logging, get_logger, log_event
from xlit.common.manifest import PACKAGE_BASE, PACKAGE_RESCORE
from xlit.central.registry import ModelRegistry, RegistryError

log = get_logger("central.publish")

LIBRARY_XLIT_VERSION = "v1.0"


def default_source_dir() -> Path | None:
    spec = importlib.util.find_spec("ai4bharat.transliteration")
    candidates = []
    if spec and spec.origin:
        candidates.append(Path(spec.origin).parent / "transformer" / "models" / "en2indic" / LIBRARY_XLIT_VERSION)
    candidates.append(Path.home() / ".AI4Bharat_Xlit_Models" / "en2indic" / LIBRARY_XLIT_VERSION)
    for c in candidates:
        if (c / "transformer" / "indicxlit.pt").is_file():
            return c
    return None


def collect_packages(source: Path, lang: str, include_rescore: bool) -> dict[str, list[tuple[Path, str]]]:
    """Map the library's on-disk layout onto manifest artifact paths.

    Artifact layout under a version dir (what the local engine loads)::

        lang_list.txt
        model/corpus-bin/dict.*.txt
        model/transformer/indicxlit.pt
        model/word_prob_dicts/<lang>_word_prob_dict.json   (rescore package)
    """
    lang_list = source.parent / "lang_list.txt"
    weights = source / "transformer" / "indicxlit.pt"
    vocab_dir = source / "corpus-bin"
    for required in (lang_list, weights, vocab_dir):
        if not required.exists():
            raise FileNotFoundError(f"missing model asset: {required}")

    base = [(lang_list, "lang_list.txt"), (weights, "model/transformer/indicxlit.pt")]
    base += [(p, f"model/corpus-bin/{p.name}") for p in sorted(vocab_dir.iterdir()) if p.is_file()]
    packages: dict[str, list[tuple[Path, str]]] = {PACKAGE_BASE: base}

    if include_rescore:
        dict_file = source / "word_prob_dicts" / f"{lang}_word_prob_dict.json"
        if not dict_file.is_file():
            raise FileNotFoundError(
                f"rescore dict not found: {dict_file} (publish with --no-rescore, or download it first)"
            )
        packages[PACKAGE_RESCORE] = [(dict_file, f"model/word_prob_dicts/{dict_file.name}")]
    return packages


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Publish a model version to the central registry.")
    p.add_argument("--registry", type=Path, default=Path("var/registry"))
    p.add_argument("--lang", default="hi")
    p.add_argument("--version", required=True, help="Immutable version id, e.g. hi-1.0.0")
    p.add_argument("--source", type=Path, default=None, help="Library model dir (.../en2indic/v1.0)")
    p.add_argument("--no-rescore", action="store_true", help="Publish base package only")
    p.add_argument("--beam-width", type=int, default=4, help="Recommended beam width for clients")
    p.add_argument("--copy", action="store_true", help="Copy files instead of hard-linking")
    p.add_argument("--no-latest", action="store_true", help="Do not move LATEST to this version")
    p.add_argument("--log-dir", type=Path, default=Path("logs"))
    args = p.parse_args(argv)

    configure_logging("central_server", args.log_dir)

    source = args.source or default_source_dir()
    if source is None:
        log_event(log, logging.ERROR, "source_not_found", hint="pass --source or warm the library once")
        return 2

    try:
        packages = collect_packages(source, args.lang, include_rescore=not args.no_rescore)
        manifest = ModelRegistry(args.registry).publish(
            lang=args.lang,
            version=args.version,
            packages=packages,
            engine={"family": "IndicXlit", "library_assets": LIBRARY_XLIT_VERSION, "beam_width": args.beam_width},
            link=not args.copy,
            make_latest=not args.no_latest,
        )
    except (FileNotFoundError, RegistryError) as e:
        log_event(log, logging.ERROR, "publish_failed", error=str(e))
        return 1

    for name, pkg in manifest.packages.items():
        print(f"  {name:<8} {len(pkg.files):>3} files  {pkg.size / 1e6:8.1f} MB")
    print(f"published {manifest.lang}/{manifest.version} -> {args.registry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
