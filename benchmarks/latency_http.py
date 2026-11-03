#!/usr/bin/env python3
"""HTTP latency / typing-load harness against a Local Xlit Service.

Records three layers per request (FINAL_DESIGN §5.2):
  * client_ms  — harness stopwatch (what the typist feels)
  * server_ms  — from the JSON response
  * compute_ms — from the JSON response

Scenarios:
  cold_cache   — distinct words, ~rps lookups/s
  prefix_walk  — a→ad→ada… then commit; exercises the prefix LRU
  burst        — all requests fire nearly together (hearing-start shape)

Usage::

    # against an already-running local service
    python -m benchmarks.latency_http --url http://127.0.0.1:8765 --scenario prefix_walk

    # self-contained (fake engine, no torch): spins central+local in-process
    python -m benchmarks.latency_http --self-test --scenario all
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORDS = [
    "mera", "adalat", "nyayalay", "vakil", "gawah", "mulzim", "shikayat",
    "faisla", "sunvai", "karyalay", "praman", "dastavez", "sarkari", "mantri",
    "vidhan", "samiti", "pradesh", "zila", "police", "thana", "namaste",
    "bharat", "hindi", "delhi", "mumbai", "patna", "lucknow", "varanasi",
]


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))]


def suggest(url: str, q: str, k: int = 4, word_start: bool = False, timeout: float = 5.0) -> dict:
    qs = urllib.parse.urlencode({"q": q, "k": k, "word_start": int(word_start)})
    req = urllib.request.Request(f"{url.rstrip('/')}/v1/suggest?{qs}", method="GET")
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            client_ms = (time.perf_counter() - t0) * 1000
            body["client_ms"] = client_ms
            body["ok"] = True
            body["status"] = resp.status
            return body
    except urllib.error.HTTPError as e:
        client_ms = (time.perf_counter() - t0) * 1000
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {"error": str(e)}
        return {"ok": False, "status": e.code, "client_ms": client_ms, **detail}
    except Exception as e:
        return {"ok": False, "status": 0, "client_ms": (time.perf_counter() - t0) * 1000, "error": str(e)}


def wait_ready(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/healthz", timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit(f"service not ready at {url} within {timeout_s}s")


def summarize(name: str, rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("ok")]
    fail = len(rows) - len(ok)
    def col(key: str) -> list[float]:
        return [float(r[key]) for r in ok if key in r and r[key] is not None]

    client = col("client_ms")
    server = col("server_ms")
    compute = col("compute_ms")
    return {
        "scenario": name,
        "requests": len(rows),
        "ok": len(ok),
        "failures": fail,
        "client_ms": {
            "p50": round(percentile(client, 0.50), 3),
            "p95": round(percentile(client, 0.95), 3),
            "p99": round(percentile(client, 0.99), 3),
            "mean": round(statistics.fmean(client), 3) if client else None,
        },
        "server_ms": {
            "p50": round(percentile(server, 0.50), 3),
            "p95": round(percentile(server, 0.95), 3),
            "mean": round(statistics.fmean(server), 3) if server else None,
        },
        "compute_ms": {
            "p50": round(percentile(compute, 0.50), 3),
            "p95": round(percentile(compute, 0.95), 3),
            "mean": round(statistics.fmean(compute), 3) if compute else None,
        },
        "derived": {
            "transport_ms_p95": round(percentile(client, 0.95) - percentile(server, 0.95), 3)
            if client and server else None,
            "queue_ms_p95": round(percentile(server, 0.95) - percentile(compute, 0.95), 3)
            if server and compute else None,
        },
        "cache_hits": sum(1 for r in ok if r.get("cached")),
    }


def scenario_cold_cache(url: str, words: list[str], rps: float, k: int) -> list[dict]:
    rows = []
    interval = 1.0 / rps if rps > 0 else 0.0
    for i, w in enumerate(words):
        rows.append(suggest(url, w, k=k, word_start=True))
        if interval and i + 1 < len(words):
            time.sleep(interval)
    return rows


def scenario_prefix_walk(url: str, words: list[str], k: int) -> list[dict]:
    rows = []
    for w in words:
        for i in range(1, len(w) + 1):
            rows.append(suggest(url, w[:i], k=k, word_start=(i == 1)))
        # simulate a second pass / backspace retype of the full word (cache hits)
        rows.append(suggest(url, w, k=k, word_start=False))
    return rows


def scenario_burst(url: str, words: list[str], k: int, workers: int) -> list[dict]:
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(suggest, url, w, k, True) for w in words]
        for f in as_completed(futs):
            rows.append(f.result())
    return rows


def start_self_test_stack() -> tuple[str, list]:
    """Spin central + local with a fake engine; return (local_url, cleanup_handles)."""
    from tests.helpers import ServerThread, fake_loader, make_fake_source
    from xlit.central.publish import collect_packages
    from xlit.central.registry import ModelRegistry
    from xlit.central.server import create_app as create_central
    from xlit.local.central_client import CentralClient
    from xlit.local.config import LocalConfig
    from xlit.local.engine_manager import EngineManager
    from xlit.local.package_store import PackageStore
    from xlit.local.server import create_app as create_local
    from xlit.local.sync_agent import SyncAgent

    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    src = make_fake_source(root / "lib")
    registry = ModelRegistry(root / "registry")
    registry.publish("hi", "hi-1.0.0", collect_packages(src, "hi", True))

    central = ServerThread(create_central(registry))
    central.__enter__()
    store = PackageStore(root / "local")
    manager = EngineManager(idle_promote_s=60)
    cfg = LocalConfig(central_url=central.url, store_dir=store.root, rescore_mode="off", redact_content=True)
    agent = SyncAgent(cfg, store, CentralClient(central.url, store.client_id(), connect_timeout_s=0.5),
                      manager, loader=fake_loader)
    agent.reconcile_once()
    local = ServerThread(create_local(cfg, manager, agent))
    local.__enter__()
    return local.url, [local, central, tmp]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="HTTP latency / typing-load harness")
    p.add_argument("--url", default="http://127.0.0.1:8765")
    p.add_argument("--scenario", default="prefix_walk",
                   choices=["cold_cache", "prefix_walk", "burst", "all"])
    p.add_argument("--rps", type=float, default=3.0, help="cold_cache target rate")
    p.add_argument("--words", type=int, default=20)
    p.add_argument("--k", type=int, default=4)
    p.add_argument("--burst-workers", type=int, default=20)
    p.add_argument("--self-test", action="store_true", help="spin fake central+local in-process")
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--ready-timeout", type=float, default=60.0)
    args = p.parse_args(argv)

    cleanup = []
    url = args.url
    if args.self_test:
        url, cleanup = start_self_test_stack()

    try:
        wait_ready(url, timeout_s=args.ready_timeout)
        words = WORDS[: max(1, args.words)]
        scenarios = ["cold_cache", "prefix_walk", "burst"] if args.scenario == "all" else [args.scenario]
        summaries = []
        for name in scenarios:
            t0 = time.perf_counter()
            if name == "cold_cache":
                rows = scenario_cold_cache(url, words, args.rps, args.k)
            elif name == "prefix_walk":
                rows = scenario_prefix_walk(url, words, args.k)
            else:
                rows = scenario_burst(url, words * max(1, args.burst_workers // max(1, len(words))),
                                     args.k, args.burst_workers)
            summary = summarize(name, rows)
            summary["wall_s"] = round(time.perf_counter() - t0, 3)
            summary["achieved_rps"] = round(summary["ok"] / summary["wall_s"], 2) if summary["wall_s"] else None
            summaries.append(summary)
            print(json.dumps(summary, indent=2))

        report = {
            "url": url,
            "self_test": args.self_test,
            "config": {"words": len(words), "k": args.k, "rps": args.rps},
            "scenarios": summaries,
            "pass_hint": "Tier A gate is client_ms p95 < 100 on the weakest supported PC (cold_cache).",
        }
        out = args.out or (ROOT / "results" / ("latency_http_selftest.json" if args.self_test else "latency_http.json"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
        print(f"wrote {out}")
        return 0
    finally:
        for obj in cleanup:
            if hasattr(obj, "__exit__"):
                obj.__exit__(None, None, None)
            elif hasattr(obj, "cleanup"):
                obj.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
