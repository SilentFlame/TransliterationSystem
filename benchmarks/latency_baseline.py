#!/usr/bin/env python3
"""Latency baseline for stock Hindi XlitEngine.

Sweeps beam widths over short/medium/long romanized words.
Records cold-start load time and warm p50/p95/p99, plus RSS.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

try:
    import resource
except ImportError:  # Windows
    resource = None

# Representative courtroom/common Hindi romanizations by length bucket.
WORDS = {
    "short": [  # 1–4 chars
        "hi",
        "mai",
        "mera",
        "tera",
        "kya",
        "nahi",
        "hai",
        "aur",
        "ab",
        "vo",
        "ye",
        "par",
        "se",
        "ko",
        "ka",
        "ki",
        "nam",
        "ram",
        "om",
        "ji",
    ],
    "medium": [  # 5–8 chars
        "namaste",
        "bharat",
        "adalat",
        "nyayalay",
        "vakil",
        "faisla",
        "sunvai",
        "gawah",
        "mulzim",
        "shikayat",
        "karyalay",
        "praman",
        "dastavez",
        "sarkari",
        "mantri",
        "vidhan",
        "samiti",
        "karyakram",
        "pradesh",
        "zila",
    ],
    "long": [  # 9+ chars
        "transliteration",
        "adhikaran",
        "nyayadhish",
        "karyapalika",
        "vidhansabha",
        "pramanpatra",
        "abhiyukta",
        "prativadi",
        "anumodan",
        "adhikarpatra",
        "karyalayin",
        "parishisht",
        "anusuchit",
        "pramanikaran",
        "sarkarnama",
        "adhikarini",
        "nyayalayon",
        "prashasanik",
        "karyakari",
        "anusandhan",
    ],
}


def rss_mb() -> float | None:
    if resource is None:
        return None
    # ru_maxrss is bytes on macOS, kilobytes on Linux
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname == "Darwin":
        return usage / (1024 * 1024)
    return usage / 1024


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def time_calls(engine, words: list[str], topk: int, repeats: int) -> list[float]:
    # Warm once so the first timed call isn't polluted by one-off cache misses.
    engine.translit_word(words[0], topk=topk)
    latencies_ms: list[float] = []
    for _ in range(repeats):
        for w in words:
            t0 = time.perf_counter()
            engine.translit_word(w, topk=topk)
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)
    return latencies_ms


def summarize(latencies_ms: list[float]) -> dict:
    s = sorted(latencies_ms)
    return {
        "n": len(s),
        "mean_ms": round(statistics.fmean(s), 3),
        "p50_ms": round(percentile(s, 50), 3),
        "p95_ms": round(percentile(s, 95), 3),
        "p99_ms": round(percentile(s, 99), 3),
        "min_ms": round(s[0], 3),
        "max_ms": round(s[-1], 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--beams", default="1,4,10", help="Comma-separated beam widths")
    parser.add_argument("--topk", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3, help="Passes over each word list")
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="Enable dictionary/LM rescoring (default: off for latency baseline)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("results/latency_baseline.json"),
        help="Where to write JSON results",
    )
    args = parser.parse_args()
    beams = [int(x) for x in args.beams.split(",") if x.strip()]

    from ai4bharat.transliteration import XlitEngine

    results: dict = {
        "config": {
            "lang": "hi",
            "beams": beams,
            "topk": args.topk,
            "repeats": args.repeats,
            "rescore": args.rescore,
            "words_per_bucket": {k: len(v) for k, v in WORDS.items()},
        },
        "cold_start": {},
        "warm": [],
    }

    print("=== Cold start (first engine load) ===", flush=True)
    t0 = time.perf_counter()
    engine = XlitEngine("hi", beam_width=beams[0], rescore=args.rescore)
    # Force first inference so download/init cost is fully captured.
    first = engine.translit_word("mera", topk=args.topk)
    cold_ms = (time.perf_counter() - t0) * 1000.0
    results["cold_start"] = {
        "beam_width": beams[0],
        "load_and_first_infer_ms": round(cold_ms, 3),
        "rss_mb": round(rss_mb() or 0, 2),
        "sample_output": first,
    }
    print(
        f"cold_start_ms={cold_ms:.1f} rss_mb={results['cold_start']['rss_mb']} out={first}",
        flush=True,
    )

    print("\n=== Warm latency sweep ===", flush=True)
    header = f"{'beam':>5} {'bucket':>8} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'mean':>8} {'rss_mb':>8}"
    print(header, flush=True)
    print("-" * len(header), flush=True)

    for beam in beams:
        # Rebuild engine when beam changes (beam_width is set at construct time).
        if beam != beams[0] or True:
            engine = XlitEngine("hi", beam_width=beam, rescore=args.rescore)
            engine.translit_word("mera", topk=args.topk)

        for bucket, words in WORDS.items():
            lats = time_calls(engine, words, topk=args.topk, repeats=args.repeats)
            stats = summarize(lats)
            row = {
                "beam_width": beam,
                "bucket": bucket,
                "topk": args.topk,
                "rescore": args.rescore,
                "rss_mb": round(rss_mb() or 0, 2),
                **stats,
            }
            results["warm"].append(row)
            print(
                f"{beam:>5} {bucket:>8} {stats['n']:>5} "
                f"{stats['p50_ms']:>8.2f} {stats['p95_ms']:>8.2f} {stats['p99_ms']:>8.2f} "
                f"{stats['mean_ms']:>8.2f} {row['rss_mb']:>8.1f}",
                flush=True,
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote {args.out}", flush=True)

    # Quick verdict vs 100ms budget (compute only; network not included).
    worst_p95 = max(r["p95_ms"] for r in results["warm"])
    print(
        f"\nWorst warm p95={worst_p95:.1f}ms "
        f"({'UNDER' if worst_p95 < 100 else 'OVER'} 100ms compute budget)",
        flush=True,
    )


if __name__ == "__main__":
    main()
