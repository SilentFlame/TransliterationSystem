# Results summary

Index of the JSON artifacts in this directory. Numbers below are taken from those files (latencies rounded to 0.1 ms). Host for the model runs: development MacBook. The 100 ms bar is client-observed p95 per lookup.

| File | What it is | Needs the model? |
|---|---|---|
| [latency_baseline.json](latency_baseline.json) | Stock Hindi `XlitEngine`, no rescore | Yes |
| [latency_baseline_rescore.json](latency_baseline_rescore.json) | Same sweep with rescore | Yes |
| [latency_http.json](latency_http.json) | HTTP harness against a warm local service (`127.0.0.1:8765`) | Yes |
| [latency_http_selftest.json](latency_http_selftest.json) | Same harness, in-process fake engine | No |
| [quality_dakshina.json](quality_dakshina.json) | Exact-match top-1 / top-4 on the 405-word sample | Yes |
| [quality_dakshina_hi_test.json](quality_dakshina_hi_test.json) | Same gate on the official Dakshina Hindi test split | Yes |
| [cost_model.json](cost_model.json) | Monthly cost at 5,000 machines | No |
| [stress_cold_direct.json](stress_cold_direct.json) | 16 fresh sessions, direct to central | Yes |
| [stress_fleet_cold_wan.json](stress_fleet_cold_wan.json) | 16 fresh sessions through the WAN emulator, then 16 typists | Yes |
| [stress_fleet_warm_offline.json](stress_fleet_warm_offline.json) | 16 restarts with the court link down, then 16 typists | Yes |
| [stress_fleet_warm_1thread.json](stress_fleet_warm_1thread.json) | Same, link up, 1 torch thread per process | Yes |
| [stress_solo_threads0.json](stress_solo_threads0.json) · [stress_solo_threads1.json](stress_solo_threads1.json) | 1 session types all 160 sentences, default vs 1 thread | Yes |

Regenerate with `make bench-latency`, `make bench-http-selftest`, `make bench-quality`, `make bench-cost`. The live HTTP file is `python -m benchmarks.latency_http --url http://127.0.0.1:8765 --scenario all`. The quality file is `python -m benchmarks.quality_dakshina --arms stock,base,rescore --package-dir var/registry/hi/hi-1.0.0 --out results/quality_dakshina.json`. Stress runs: `make central`, `make wan-proxy`, then `make bench-fleet` (commands for each file in [Fleet stress](#fleet-stress)).

---

## Engine compute

Interactive default is beam 4. Each warm cell is 60 calls (20 words × 3 repeats). Cold start is load plus first inference at beam 1.

**No rescore** — RSS about 0.52 GB warm. Cold start **2.6 s**, RSS 444 MB.

| Beam | Bucket | p50 | p95 | p99 |
|---|---|---|---|---|
| 4 | short | 12.7 ms | 15.3 ms | 15.6 ms |
| 4 | medium | 21.3 ms | 30.3 ms | 33.5 ms |
| 4 | long | 28.9 ms | 40.1 ms | 40.9 ms |
| 1 | short / medium / long | 8.4 / 13.8 / 19.6 ms | 10.6 / 18.6 / 25.5 ms | 10.7 / 37.5 / 27.8 ms |
| 10 | short / medium / long | 12.7 / 23.3 / 35.4 ms | 15.6 / 37.0 / 48.8 ms | 16.0 / 63.4 / 51.1 ms |

**Rescore** — RSS about 1.73 GB warm. Warm beam-4 p95 stays in the same band (short 14.8 ms, medium 31.8 ms, long 42.9 ms). The JSON cold start is **68.9 s**. That figure includes a first-run download of the library's all-language dictionary archive. Loading only the Hindi dictionary from a local package is about 2.3 s ([docs/REPORT.md](../docs/REPORT.md)).

---

## HTTP lookups

Three layers: `client_ms` is what the typist sees, `server_ms` is the API, `compute_ms` is the engine. `client − server` is transport; `server − compute` is queueing.

### Live service (`latency_http.json`)

`self_test: false`. 20 words, k=4, 3 lookups/s. Zero failures.

| Scenario | n | Client p50 | Client p95 | Client p99 | Compute p95 | Cache hits |
|---|---|---|---|---|---|---|
| cold_cache | 20 | 30.6 ms | **40.3 ms** | 48.2 ms | 37.5 ms | 0 |
| prefix_walk | 143 | 12.5 ms | **23.3 ms** | 30.7 ms | 22.1 ms | 57 |
| burst | 20 | 5.2 ms | **6.1 ms** | 6.5 ms | 0.0 ms | 20 |

Cold-cache transport p95 is 2.7 ms and queue p95 is 0.1 ms, so almost all of the 40 ms is model compute. The burst is entirely cache hits. This is a short MacBook run, not the weakest-PC gate and not a 5-minute soak.

### Fake-engine self-test (`latency_http_selftest.json`)

`self_test: true`. Same scenario shapes, compute is a stub. Latest re-run: cold-cache client p95 is **1.6 ms** (it was 3.9 ms on 3 November). These numbers check the harness. They are not the latency bar, and they move between runs.

---

## Quality (`quality_dakshina.json`)

Exact string match, Hindi, beam 4, top-4. Sample: `data/dakshina_hi_sample.tsv` (405 pairs). Courtroom names: `data/courtroom_ne.tsv` (49 rows). Package: `var/registry/hi/hi-1.0.0`.

| Arm | Sample top-1 | Sample top-4 | NE top-1 | NE top-4 | Sample compute p95 |
|---|---|---|---|---|---|
| stock | 78.77% | 94.07% | 75.51% | 85.71% | 33.3 ms |
| packaged base | 78.77% | 94.07% | 75.51% | 85.71% | 31.9 ms |
| packaged rescore | 86.91% | 94.07% | 79.59% | 85.71% | 32.1 ms |

Gate: `packaging_identical: true` (base candidate lists equal stock). `rescore_not_worse_than_base: true`. This sample is a curated packaging check. The published 60.56 is the official test split, scored below.

## Quality, official Dakshina Hindi test (`quality_dakshina_hi_test.json`)

Latest change. Lexicon: `data/dakshina/hi.translit.sampled.test.tsv` (4,502 attested pairs, 2,500 words; native, roman, count). Exact string match, beam 4, top-4. Package: `var/registry/hi/hi-1.0.0`. Each attested pair is one example.

| Arm | Top-1 | Top-4 |
|---|---|---|
| stock | 60.33% | 84.65% |
| packaged base | 60.33% | 84.65% |
| packaged rescore | 72.26% | 84.65% |

`packaging_identical: true`. Published IndicXlit Hindi top-1 on this benchmark is 60.56%. Fetch and rerun: `bash scripts/fetch_dakshina_hi.sh`, then the command in the README.

---

## Fleet stress

`benchmarks/fleet_stress.py` starts N real `xlit.local.server` processes at once (own port, package store, log dir), then N typists. Each typist replays its own slice of `data/stress_roman_hi.tsv` (160 roman Hindi sentences, 902 words, 4,045 keystrokes; half courtroom, half everyday and names) one keystroke at a time: a lookup per key at ~180 ms ± 60 ms, a commit per word. All runs: 16 sessions, beam 4, base tier (16 × 1.7 GB rescore engines do not fit in 18 GB), Apple M3 Pro, 11 cores.

`benchmarks/wan_proxy.py` sits between the sessions and central for the WAN runs: 60 ms RTT, 10 ms jitter, **100 Mbps down shared by every session**, 20 Mbps up, a reset roughly every 500 MB. `kill -USR1` on the proxy cuts the link.

All 16 engines share one laptop's cores, so this measures a court node with 16 typists, or 16 weak PCs. Tier A puts each engine on its own PC.

Numbers below are the **latest** re-run (11:23–11:32 IST). The laptop was not idle. An earlier run on 3 November was faster in the tail; the direction of every result is the same.

### Cold start

| Run | Fleet serving | Per-session first suggestion (p50) | Engine load (p50) |
|---|---|---|---|
| Fresh install, direct to central | 21.0 s | 19.5 s | 14.1 s |
| **Fresh install, through the court link** | **194 s** | 192 s | 12.2 s |
| Restart from local store, **link down** | 11.6 s | 10.5 s | 10.5 s |
| Restart from local store, 1 torch thread | 14.0 s | 12.3 s | 12.3 s |
| Single session restart (for scale) | 3.4 s | 3.0 s | 3.0 s |

Through the court link, 16 × 138.3 MB ≈ 2.2 GB has to cross a 100 Mbps uplink, so every session waits about three minutes. Download time dominates the cold start. Three downloads were reset mid-stream. All three resumed with HTTP Range, and there were zero failed lookups afterward. With the link down, all 16 sessions booted from disk (`ttfs_source: local_cache`) and served normally.

### Typing (16 concurrent typists, 4,045 keystrokes per run)

| Run | Client p50 | Client p95 | Client p99 | Max | > 100 ms | First keystroke p50 |
|---|---|---|---|---|---|---|
| After WAN install, default threads | 29.4 ms | 71.9 ms | 193.5 ms | 322.6 ms | 2.84% | 259.9 ms |
| Link down, default threads | 29.4 ms | 64.7 ms | 110.4 ms | 202.6 ms | 1.53% | 149.4 ms |
| **Link up, 1 torch thread / process** | **16.1 ms** | **29.2 ms** | **53.3 ms** | 272.7 ms | **0.40%** | 204.0 ms |

Zero failed lookups in any run. Aggregate load is about 59 lookups/s (~4 keys/s per typist), with a 29.8% prefix-cache hit rate. The tail is the first keystroke, when all 16 typists start together. After that first key, p99 is 102 ms with default threads and 44 ms with one thread.

One thread per process cuts p95 from 65 ms to 29 ms. On this re-run it did not remove every lookup over 100 ms: 0.4% still crossed it, and those are the simultaneous first keys. On a single PC it is no slower (below), so a court node should pin it.

### Solo baseline (1 session, all 160 sentences, no think time)

| Torch threads | Client p95 | Client p99 | Max | Cache-miss compute p95 | CPU (ps mean) |
|---|---|---|---|---|---|
| default | 26.7 ms | 37.1 ms | 189.6 ms | 30.6 ms | 284% |
| 1 | 21.5 ms | 26.7 ms | 52.4 ms | 23.3 ms | 6% |

One cache covers every repeated word here, so 1,345 of 4,045 lookups miss (vs 2,840 across 16 separate caches). Compare cache-miss compute, not client p50.

### Running-text quality

Final-prefix candidates vs the gold word, identical in every run (contention changes latency, not output): **word top-1 82.48%, top-4 98.12%** over 902 words. Most misses are valid variant spellings: गये for गए, नही for नहीं, मा for मां, किराएदार for किरायेदार. Two are real misses: `lucknow` → लकनो (gold लखनऊ) and `woh` → वोह (gold वह). The gold mostly omits nukta, so this is a conservative number.

### Memory

Peak RSS for one warm session is 481 MB. Under 16 sessions, macOS compresses idle pages, so `ps` shows 245–455 MB per process and a fleet peak of 3.6–4.4 GB. Size from the solo figure: 16 × ~0.5 GB ≈ 8 GB for base engines.

### Commands

```bash
make central                 # terminal 1: registry on :8700
make wan-proxy               # terminal 2: court link on :8701
python -m benchmarks.fleet_stress --sessions 16 --fresh --central-url http://127.0.0.1:8700 --typists 0 --out results/stress_cold_direct.json
python -m benchmarks.fleet_stress --sessions 16 --fresh --out results/stress_fleet_cold_wan.json
kill -USR1 $(pgrep -f benchmarks.wan_proxy)   # link down
python -m benchmarks.fleet_stress --sessions 16 --out results/stress_fleet_warm_offline.json
kill -USR1 $(pgrep -f benchmarks.wan_proxy)   # link up
python -m benchmarks.fleet_stress --sessions 16 --torch-threads 1 --out results/stress_fleet_warm_1thread.json
python -m benchmarks.fleet_stress --sessions 1 --key-ms 0 --word-gap-ms 0 --torch-threads 0 --out results/stress_solo_threads0.json
python -m benchmarks.fleet_stress --sessions 1 --key-ms 0 --word-gap-ms 0 --torch-threads 1 --out results/stress_solo_threads1.json
```

---

## Cost (`cost_model.json`)

Assumptions stated in the file: 5,000 machines, 200 courts, base 138.3 MB, rescore 358.1 MB, 1 release/month, CDN $0.085/GB, central VM $30/month, object storage $1/month. Prices are estimates.

| Scenario | Egress | $/month | $/1,000 users |
|---|---|---|---|
| Every machine pulls base + rescore | 2,424 GB | 237.03 | 47.41 |
| Weights-only (dictionary reused) | 675 GB | 88.40 | 17.68 |
| Court mirrors, full package | 97 GB | **39.24** | **7.85** |
| Court mirrors, weights-only | 27 GB | 33.30 | 6.66 |
| One always-on GPU instance (rejected) | — | 380.00 | 76.00 |

Chosen design in the file: Tier A local inference + central registry + court mirrors, **$39.24/month** central. Tier B is modeled separately: 15% of courts fail the hardware gate → 60 boxes × $400 capex, amortized over 36 months = **$666.67/month**. That capex is not in the central bill.

At 10× (50,000 machines, same 200 courts, mirrors, full package): **$113.41/month** central ($2.27 / 1,000 users). Egress grows with courts, not with typists.
