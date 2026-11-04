#!/usr/bin/env python3
"""Fleet stress test: N real Local Xlit Services started at once, then N typists.

Each session is a separate ``xlit.local.server`` process with its own port, package
store and log dir — the same thing a typist PC runs. They all start together, so
the run measures:

  * cold start under contention: process start → ``/healthz`` ready, plus the
    in-process ``time_to_first_suggestion``, package download time and engine load
    time from each service's JSON log
  * resource use: per-process peak RSS and CPU% (sampled with ``ps``)
  * typing: every session replays its own slice of ``--dataset`` keystroke by
    keystroke (prefix lookup per key, commit event per word), concurrently
  * running-text quality: final-prefix candidates vs the gold Devanagari word

On one laptop all N engines share the same CPU cores, so this is closer to a
court node serving N typists (or N weak PCs) than to N independent Tier A PCs.

Usage::

    # fresh install of 16 sessions through the WAN emulator, then 16 typists
    python -m benchmarks.fleet_stress --sessions 16 --fresh \\
        --central-url http://127.0.0.1:8701 --out results/stress_fleet_cold_wan.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * (len(s) - 1) + 0.5))]


def dist(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "p50": round(percentile(xs, 0.50), 2),
        "p95": round(percentile(xs, 0.95), 2),
        "p99": round(percentile(xs, 0.99), 2),
        "max": round(max(xs), 2),
        "mean": round(statistics.fmean(xs), 2),
    }


def load_sentences(path: Path) -> list[tuple[list[str], list[str]]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        roman, native = line.split("\t")
        rw, nw = roman.split(), native.split()
        if len(rw) != len(nw):
            raise SystemExit(f"misaligned row in {path}: {line!r}")
        rows.append((rw, nw))
    return rows


class Session:
    def __init__(self, idx: int, port: int, work: Path) -> None:
        self.idx = idx
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.dir = work / f"s{idx:02d}"
        self.store = self.dir / "store"
        self.logs = self.dir / "logs"
        self.proc: subprocess.Popen | None = None
        self.spawned_at = 0.0
        self.ready_at: float | None = None
        self.rss_peak_mb = 0.0
        self.cpu_samples: list[float] = []

    def start(self, args: argparse.Namespace, env: dict) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable, "-m", "xlit.local.server",
            "--central-url", args.central_url, "--port", str(self.port),
            "--store-dir", str(self.store), "--log-dir", str(self.logs),
            "--rescore", args.rescore, "--poll-interval", str(args.poll_interval),
        ]
        out = (self.dir / "stdout.log").open("w")
        self.spawned_at = time.time()
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)

    def log_events(self) -> list[dict]:
        path = self.logs / "local_client.log"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def wait_all_ready(sessions: list[Session], timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    pending = {s.idx: s for s in sessions}
    while pending and time.time() < deadline:
        for idx, s in list(pending.items()):
            if s.proc and s.proc.poll() is not None:
                raise SystemExit(f"session {idx} exited early (code {s.proc.returncode}); see {s.dir}/stdout.log")
            try:
                r = requests.get(f"{s.url}/healthz", timeout=0.5)
                if r.status_code == 200:
                    s.ready_at = time.time()
                    del pending[idx]
            except requests.RequestException:
                pass
        time.sleep(0.25)
    if pending:
        raise SystemExit(f"sessions not ready after {timeout_s}s: {sorted(pending)}")


class ResourceSampler(threading.Thread):
    def __init__(self, sessions: list[Session], interval_s: float = 1.0) -> None:
        super().__init__(daemon=True)
        self.sessions = sessions
        self.interval_s = interval_s
        self.stop_evt = threading.Event()
        self.total_rss_peak_mb = 0.0

    def run(self) -> None:
        by_pid = {}
        while not self.stop_evt.is_set():
            by_pid = {s.proc.pid: s for s in self.sessions if s.proc and s.proc.poll() is None}
            if by_pid:
                out = subprocess.run(
                    ["ps", "-o", "pid=,rss=,pcpu=", "-p", ",".join(map(str, by_pid))],
                    capture_output=True, text=True,
                ).stdout
                total = 0.0
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) != 3:
                        continue
                    pid, rss_kb, cpu = int(parts[0]), float(parts[1]), float(parts[2])
                    s = by_pid.get(pid)
                    if s is None:
                        continue
                    rss_mb = rss_kb / 1024
                    s.rss_peak_mb = max(s.rss_peak_mb, rss_mb)
                    s.cpu_samples.append(cpu)
                    total += rss_mb
                self.total_rss_peak_mb = max(self.total_rss_peak_mb, total)
            self.stop_evt.wait(self.interval_s)


def cold_start_report(sessions: list[Session]) -> dict:
    per = []
    for s in sessions:
        ev = s.log_events()
        ttfs = next((e for e in ev if e.get("event") == "time_to_first_suggestion"), {})
        dl = next((e for e in ev if e.get("event") == "package_download_finished"), {})
        load = next((e for e in ev if e.get("event") == "engine_load_finished"), {})
        resumed = sum(1 for e in ev if e.get("event") == "file_downloaded" and e.get("resumed_from"))
        offline = sum(1 for e in ev if e.get("event") == "central_offline")
        per.append({
            "session": s.idx,
            "ready_s": round(s.ready_at - s.spawned_at, 2) if s.ready_at else None,
            "ttfs_ms": ttfs.get("ms"),
            "ttfs_source": ttfs.get("source"),
            "download_ms": dl.get("duration_ms"),
            "engine_load_ms": load.get("load_ms"),
            "resumed_downloads": resumed,
            "central_offline_events": offline,
        })

    def col(key: str) -> list[float]:
        return [float(p[key]) for p in per if p.get(key) is not None]

    return {
        "ready_s": dist(col("ready_s")),
        "ttfs_ms": dist(col("ttfs_ms")),
        "download_ms": dist(col("download_ms")),
        "engine_load_ms": dist(col("engine_load_ms")),
        "ttfs_sources": sorted({p["ttfs_source"] for p in per if p["ttfs_source"]}),
        "resumed_downloads_total": sum(p["resumed_downloads"] for p in per),
        "central_offline_events_total": sum(p["central_offline_events"] for p in per),
        "per_session": per,
    }


def type_slice(session: Session, sentences: list[tuple[list[str], list[str]]], args: argparse.Namespace,
               t_start: float, out: dict) -> None:
    rng = random.Random(args.seed + session.idx)
    http = requests.Session()
    keys: list[dict] = []
    words: list[dict] = []
    while time.time() < t_start:
        time.sleep(0.005)
    for roman_words, gold_words in sentences:
        for roman, gold in zip(roman_words, gold_words):
            last: dict = {}
            for i in range(1, len(roman) + 1):
                t_key = time.perf_counter()
                try:
                    r = http.get(f"{session.url}/v1/suggest",
                                 params={"q": roman[:i], "k": 4, "word_start": int(i == 1)}, timeout=10)
                    client_ms = (time.perf_counter() - t_key) * 1000
                    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    ok = r.status_code == 200
                except requests.RequestException as e:
                    client_ms = (time.perf_counter() - t_key) * 1000
                    body, ok = {"error": str(e)}, False
                keys.append({
                    "ok": ok, "client_ms": client_ms, "server_ms": body.get("server_ms"),
                    "compute_ms": body.get("compute_ms"), "cached": body.get("cached"), "q_len": i,
                })
                if ok:
                    last = body
                if args.key_ms:
                    gap = max(0.0, rng.gauss(args.key_ms, args.key_jitter_ms)) / 1000
                    spent = time.perf_counter() - t_key
                    if gap > spent:
                        time.sleep(gap - spent)
            cands = last.get("candidates") or []
            rank = cands.index(gold) if gold in cands else None
            words.append({"roman": roman, "gold": gold, "top1": cands[:1] == [gold], "topk": rank is not None,
                          "pred": cands[0] if cands else None})
            try:
                http.post(f"{session.url}/v1/events", timeout=5, json={
                    "type": "commit", "source": "space", "rank": rank if rank is not None else 0,
                    "shown": len(cands), "roman": roman, "chosen": cands[0] if cands else roman,
                    "session": f"stress-{session.idx}",
                })
            except requests.RequestException:
                pass
            if args.word_gap_ms:
                time.sleep(args.word_gap_ms / 1000)
    out[session.idx] = {"keys": keys, "words": words, "wall_s": time.time() - t_start}


def typing_report(results: dict, sessions: list[Session]) -> dict:
    all_keys = [k for r in results.values() for k in r["keys"]]
    ok = [k for k in all_keys if k["ok"]]
    client = [k["client_ms"] for k in ok]
    misses = [k for k in ok if not k["cached"]]
    words = [w for r in results.values() for w in r["words"]]
    wall = max((r["wall_s"] for r in results.values()), default=0.0)
    per_session = []
    for s in sessions:
        r = results.get(s.idx)
        if not r:
            continue
        c = [k["client_ms"] for k in r["keys"] if k["ok"]]
        per_session.append({
            "session": s.idx,
            "keystrokes": len(r["keys"]),
            "words": len(r["words"]),
            "client_p50_ms": round(percentile(c, 0.5), 2),
            "client_p95_ms": round(percentile(c, 0.95), 2),
            "client_max_ms": round(max(c), 2) if c else None,
            "first_key_ms": round(r["keys"][0]["client_ms"], 2) if r["keys"] else None,
            "keys_per_s": round(len(r["keys"]) / r["wall_s"], 2) if r["wall_s"] else None,
            "word_top1": round(sum(w["top1"] for w in r["words"]) / max(1, len(r["words"])), 4),
        })
    misses_sample = [w for w in words if not w["topk"]][:25]
    return {
        "typists": len(results),
        "keystrokes": len(all_keys),
        "failures": len(all_keys) - len(ok),
        "wall_s": round(wall, 1),
        "aggregate_lookups_per_s": round(len(all_keys) / wall, 1) if wall else None,
        "cache_hit_rate": round(sum(1 for k in ok if k["cached"]) / max(1, len(ok)), 4),
        "client_ms": dist(client),
        "client_ms_after_first_key": dist([k["client_ms"] for r in results.values() for k in r["keys"][1:] if k["ok"]]),
        "first_key_ms": dist([r["keys"][0]["client_ms"] for r in results.values() if r["keys"]]),
        "client_ms_cache_miss": dist([k["client_ms"] for k in misses]),
        "server_ms": dist([float(k["server_ms"]) for k in ok if k["server_ms"] is not None]),
        "compute_ms_cache_miss": dist([float(k["compute_ms"]) for k in misses if k["compute_ms"] is not None]),
        "share_over_50ms": round(sum(1 for c in client if c > 50) / max(1, len(client)), 4),
        "share_over_100ms": round(sum(1 for c in client if c > 100) / max(1, len(client)), 4),
        "words": len(words),
        "word_top1": round(sum(w["top1"] for w in words) / max(1, len(words)), 4),
        "word_top4": round(sum(w["topk"] for w in words) / max(1, len(words)), 4),
        "per_session": per_session,
        "miss_sample": misses_sample,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fleet cold-start + concurrent typing stress test")
    p.add_argument("--sessions", type=int, default=16)
    p.add_argument("--base-port", type=int, default=8800)
    p.add_argument("--central-url", default="http://127.0.0.1:8701")
    p.add_argument("--work-dir", type=Path, default=ROOT / "var" / "fleet")
    p.add_argument("--fresh", action="store_true", help="wipe every session's package store first")
    p.add_argument("--rescore", choices=["auto", "on", "off"], default="off",
                   help="off by default: N × 1.7 GB rescore engines do not fit on one laptop")
    p.add_argument("--poll-interval", type=float, default=300.0)
    p.add_argument("--torch-threads", type=int, default=0, help="OMP/MKL threads per process (0 = library default)")
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "stress_roman_hi.tsv")
    p.add_argument("--limit-sentences", type=int, default=0, help="use only the first N rows (smoke runs)")
    p.add_argument("--typists", type=int, default=-1, help="-1 = one per session, 0 = cold start only")
    p.add_argument("--key-ms", type=float, default=180.0, help="mean inter-keystroke gap; 0 = as fast as possible")
    p.add_argument("--key-jitter-ms", type=float, default=60.0)
    p.add_argument("--word-gap-ms", type=float, default=250.0)
    p.add_argument("--ready-timeout", type=float, default=1200.0)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--label", default="")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    sentences = load_sentences(args.dataset)
    if args.limit_sentences:
        sentences = sentences[: args.limit_sentences]
    order = list(range(len(sentences)))
    random.Random(args.seed).shuffle(order)

    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
    if args.torch_threads:
        env.update(OMP_NUM_THREADS=str(args.torch_threads), MKL_NUM_THREADS=str(args.torch_threads))

    sessions = [Session(i, args.base_port + i, args.work_dir) for i in range(args.sessions)]
    if args.fresh:
        for s in sessions:
            shutil.rmtree(s.dir, ignore_errors=True)
    for s in sessions:
        shutil.rmtree(s.logs, ignore_errors=True)

    sampler = ResourceSampler(sessions)
    t0 = time.time()
    for s in sessions:
        s.start(args, env)
    sampler.start()
    report: dict = {"label": args.label, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        print(f"started {len(sessions)} sessions; waiting for /healthz …", flush=True)
        wait_all_ready(sessions, args.ready_timeout)
        fleet_ready_s = max(s.ready_at for s in sessions) - t0
        print(f"all ready in {fleet_ready_s:.1f}s", flush=True)
        time.sleep(2.0)  # let the last engine_load / ttfs lines flush
        report["cold_start"] = {"fleet_ready_s": round(fleet_ready_s, 2), **cold_start_report(sessions)}

        typists = args.sessions if args.typists < 0 else min(args.typists, args.sessions)
        if typists:
            slices = {s.idx: [sentences[j] for j in order[s.idx::typists]] for s in sessions[:typists]}
            results: dict = {}
            t_start = time.time() + 1.0
            threads = [threading.Thread(target=type_slice, args=(s, slices[s.idx], args, t_start, results))
                       for s in sessions[:typists]]
            print(f"{typists} typists replaying {sum(len(v) for v in slices.values())} sentences …", flush=True)
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            report["typing"] = typing_report(results, sessions[:typists])
    finally:
        sampler.stop_evt.set()
        sampler.join(timeout=3)
        for s in sessions:
            s.stop()

    report["resources"] = {
        "fleet_rss_peak_mb": round(sampler.total_rss_peak_mb, 1),
        "per_session_rss_peak_mb": dist([s.rss_peak_mb for s in sessions]),
        "per_session_cpu_pct_mean": dist([statistics.fmean(s.cpu_samples) for s in sessions if s.cpu_samples]),
    }
    report["config"] = {
        "sessions": args.sessions, "central_url": args.central_url, "fresh": args.fresh,
        "rescore": args.rescore, "torch_threads": args.torch_threads or "default",
        "dataset": str(args.dataset.relative_to(ROOT)) if args.dataset.is_relative_to(ROOT) else str(args.dataset),
        "sentences": len(sentences), "typists": args.sessions if args.typists < 0 else args.typists,
        "key_ms": args.key_ms, "key_jitter_ms": args.key_jitter_ms, "word_gap_ms": args.word_gap_ms,
        "host_cpus": os.cpu_count(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    cs = report["cold_start"]
    print(json.dumps({
        "fleet_ready_s": cs["fleet_ready_s"], "ttfs_ms": cs["ttfs_ms"], "engine_load_ms": cs["engine_load_ms"],
        "typing_client_ms": report.get("typing", {}).get("client_ms"),
        "word_top1": report.get("typing", {}).get("word_top1"), "resources": report["resources"],
    }, indent=2), flush=True)
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
