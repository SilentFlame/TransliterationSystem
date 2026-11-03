#!/usr/bin/env python3
"""Quality gate: top-1 / top-k exact-match vs a roman→Indic lexicon sample.

Arms (any subset):
  * stock   — library ``XlitEngine`` (baseline the packaging must match)
  * base    — packaged engine from a local store version (rescore=False)
  * rescore — packaged engine with the Hindi dict loaded

Pass criteria (see docs/FINAL_DESIGN.md §5.1):
  1. packaged base ≡ stock on the same inputs (candidate lists identical)
  2. absolute top-1 is a reference point (published Hindi Dakshina ≈ 60.56);
     our sample may differ — the gate is the delta, not the absolute
  3. rescore must not be worse than base on top-1

Usage::

    # against stock only (needs warm library model)
    python -m benchmarks.quality_dakshina --arms stock

    # stock + packaged engines from an installed local store version
    python -m benchmarks.quality_dakshina \\
        --arms stock,base,rescore \\
        --package-dir var/local_store/packages/hi/hi-1.0.0

Writes JSON to results/quality_*.json.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_pairs(path: Path) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            raise SystemExit(f"bad lexicon line in {path}: {line!r}")
        pairs.append((parts[0].strip().lower(), parts[1].strip()))
    return pairs


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))]


def score_arm(name: str, suggest, pairs: list[tuple[str, str]], topk: int) -> dict:
    hits1 = hits_k = 0
    latencies: list[float] = []
    mismatches: list[dict] = []
    for roman, gold in pairs:
        t0 = time.perf_counter()
        cands = list(suggest(roman, topk))[:topk]
        latencies.append((time.perf_counter() - t0) * 1000)
        if cands and cands[0] == gold:
            hits1 += 1
        if gold in cands:
            hits_k += 1
        else:
            mismatches.append({"roman": roman, "gold": gold, "top": cands[:3]})
    n = len(pairs)
    return {
        "arm": name,
        "n": n,
        "topk": topk,
        "top1": round(hits1 / n, 4) if n else 0.0,
        f"top{topk}": round(hits_k / n, 4) if n else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
        },
        "miss_sample": mismatches[:15],
        "candidates": None,  # filled optionally for identity check
    }


def make_stock(lang: str, beam: int, rescore: bool):
    from ai4bharat.transliteration import XlitEngine

    eng = XlitEngine(lang, beam_width=beam, rescore=rescore)

    def suggest(word: str, topk: int):
        return eng.translit_word(word, lang_code=lang, topk=topk)

    return suggest


def make_packaged(package_dir: Path, lang: str, beam: int, tier: str):
    from xlit.local.engine import LoadRequest, load_engine

    models = package_dir / "model"
    if not models.is_dir():
        raise SystemExit(f"package model dir missing: {models}")
    engine = load_engine(
        LoadRequest(lang=lang, version=package_dir.name, tier=tier, models_path=models, beam_width=beam)
    )
    return lambda word, topk: engine.suggest(word, topk)


def candidate_lists(suggest, pairs: list[tuple[str, str]], topk: int) -> list[list[str]]:
    return [list(suggest(r, topk))[:topk] for r, _ in pairs]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="IndicXlit quality gate (exact-match top-k)")
    p.add_argument("--lexicon", type=Path, default=ROOT / "data" / "dakshina_hi_sample.tsv")
    p.add_argument("--ne", type=Path, default=ROOT / "data" / "courtroom_ne.tsv")
    p.add_argument("--arms", default="stock", help="comma list: stock,base,rescore,stock_rescore")
    p.add_argument("--package-dir", type=Path, default=None, help="…/packages/hi/<version>")
    p.add_argument("--lang", default="hi")
    p.add_argument("--beam", type=int, default=4)
    p.add_argument("--topk", type=int, default=4)
    p.add_argument("--limit", type=int, default=0, help="score only the first N lexicon rows (0=all)")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--strict-ne", action="store_true", help="fail if NE top-k regresses vs stock")
    p.add_argument("--fail-on-packaging-diff", action="store_true", default=True,
                   help="exit 2 if base ≠ stock candidate lists (default on when both present)")
    args = p.parse_args(argv)

    pairs = load_pairs(args.lexicon)
    if args.limit:
        pairs = pairs[: args.limit]
    ne_pairs = load_pairs(args.ne) if args.ne.is_file() else []
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    builders = {}
    if "stock" in arms:
        builders["stock"] = lambda: make_stock(args.lang, args.beam, False)
    if "stock_rescore" in arms:
        builders["stock_rescore"] = lambda: make_stock(args.lang, args.beam, True)
    if "base" in arms:
        if not args.package_dir:
            raise SystemExit("--package-dir required for arm=base")
        builders["base"] = lambda: make_packaged(args.package_dir, args.lang, args.beam, "base")
    if "rescore" in arms:
        if not args.package_dir:
            raise SystemExit("--package-dir required for arm=rescore")
        builders["rescore"] = lambda: make_packaged(args.package_dir, args.lang, args.beam, "rescore")

    results: dict[str, dict] = {}
    lists: dict[str, list[list[str]]] = {}
    for name, build in builders.items():
        print(f"loading arm={name} …", flush=True)
        suggest = build()
        print(f"scoring arm={name} n={len(pairs)} …", flush=True)
        results[name] = score_arm(name, suggest, pairs, args.topk)
        if name in ("stock", "base"):
            lists[name] = candidate_lists(suggest, pairs, args.topk)
        if ne_pairs:
            results[name]["ne"] = score_arm(f"{name}/ne", suggest, ne_pairs, args.topk)

    packaging_identical = None
    if "stock" in lists and "base" in lists:
        packaging_identical = lists["stock"] == lists["base"]
        results["packaging_identical"] = packaging_identical
        if not packaging_identical:
            diffs = [
                {"i": i, "roman": pairs[i][0], "stock": lists["stock"][i], "base": lists["base"][i]}
                for i in range(len(pairs))
                if lists["stock"][i] != lists["base"][i]
            ]
            results["packaging_diffs"] = diffs[:20]

    gate = {
        "packaging_identical": packaging_identical,
        "rescore_not_worse_than_base": None,
        "published_hi_dakshina_top1_reference": 0.6056,
    }
    if "base" in results and "rescore" in results:
        gate["rescore_not_worse_than_base"] = results["rescore"]["top1"] >= results["base"]["top1"] - 1e-9
    if "stock" in results and "stock_rescore" in results:
        gate["stock_rescore_vs_stock"] = {
            "delta_top1": round(results["stock_rescore"]["top1"] - results["stock"]["top1"], 4),
        }

    def _rel(path: Path | None) -> str | None:
        if path is None:
            return None
        resolved = path.resolve()
        try:
            return str(resolved.relative_to(ROOT))
        except ValueError:
            return str(resolved)

    report = {
        "config": {
            "lexicon": _rel(args.lexicon),
            "ne": _rel(args.ne) if ne_pairs else None,
            "arms": arms,
            "lang": args.lang,
            "beam": args.beam,
            "topk": args.topk,
            "n": len(pairs),
            "package_dir": _rel(args.package_dir) if args.package_dir else None,
        },
        "arms": {k: v for k, v in results.items() if isinstance(v, dict) and "arm" in v},
        "gate": gate,
        "notes": [
            "Metric is exact string match (IndicXlit / Aksharantar reporting), not Dakshina CER/WER.",
            "Absolute top-1 on this sample is not comparable to the published 60.56 without the full Dakshina test split.",
            "The packaging gate is stock vs base candidate-list identity on the same inputs.",
        ],
    }

    out = args.out or (ROOT / "results" / f"quality_{'_'.join(arms)}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"wrote": str(out), "gate": gate, "summary": {
        k: {"top1": v["top1"], f"top{args.topk}": v[f"top{args.topk}"]}
        for k, v in report["arms"].items()
    }}, ensure_ascii=False, indent=2))

    if packaging_identical is False and args.fail_on_packaging_diff:
        return 2
    if gate.get("rescore_not_worse_than_base") is False:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
