# Results summary

Index of the JSON artifacts in this directory. Numbers below are taken from those files (latencies rounded to 0.1 ms). Host for the model runs: development MacBook. The 100 ms bar is client-observed p95 per lookup.

| File | What it is | Needs the model? |
|---|---|---|
| [latency_baseline.json](latency_baseline.json) | Stock Hindi `XlitEngine`, no rescore | Yes |
| [latency_baseline_rescore.json](latency_baseline_rescore.json) | Same sweep with rescore | Yes |
| [latency_http.json](latency_http.json) | HTTP harness against a warm local service (`127.0.0.1:8765`) | Yes |
| [latency_http_selftest.json](latency_http_selftest.json) | Same harness, in-process fake engine | No |
| [quality_dakshina.json](quality_dakshina.json) | Exact-match top-1 / top-4 gate | Yes |
| [cost_model.json](cost_model.json) | Monthly cost at 5,000 machines | No |

Regenerate with `make bench-latency`, `make bench-http-selftest`, `make bench-quality`, `make bench-cost`. The live HTTP file is `python -m benchmarks.latency_http --url http://127.0.0.1:8765 --scenario all`. The quality file is `python -m benchmarks.quality_dakshina --arms stock,base,rescore --package-dir var/registry/hi/hi-1.0.0 --out results/quality_dakshina.json`.

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

`self_test: true`. Same scenario shapes, compute is a stub. Cold-cache client p95 is **3.9 ms**. These numbers check the harness. They are not the latency bar, and they move slightly between machines.

---

## Quality (`quality_dakshina.json`)

Exact string match, Hindi, beam 4, top-4. Sample: `data/dakshina_hi_sample.tsv` (405 pairs). Courtroom names: `data/courtroom_ne.tsv` (49 rows). Package: `var/registry/hi/hi-1.0.0`.

| Arm | Sample top-1 | Sample top-4 | NE top-1 | NE top-4 | Sample compute p95 |
|---|---|---|---|---|---|
| stock | 78.77% | 94.07% | 75.51% | 85.71% | 33.3 ms |
| packaged base | 78.77% | 94.07% | 75.51% | 85.71% | 31.9 ms |
| packaged rescore | 86.91% | 94.07% | 79.59% | 85.71% | 32.1 ms |

Gate: `packaging_identical: true` (base candidate lists equal stock). `rescore_not_worse_than_base: true`. Published Hindi Dakshina top-1 is 60.56 on the official split. This sample is a curated subset, so 78.77% is the packaging gate, not that published number.

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
