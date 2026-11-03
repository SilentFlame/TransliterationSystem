# Report — IndicXlit Courtroom Transliteration Runtime

Assignment write-up. Design detail lives in [FINAL_DESIGN.md](FINAL_DESIGN.md);
module contracts in [HLD.md](HLD.md) / [LLD.md](LLD.md); open items in
[OPEN_QUESTIONS.md](OPEN_QUESTIONS.md). Commands: [../README.md](../README.md).

---

## 1. Workload and what 100 ms demands

Typists enter romanized Hindi (`mera`) and pick from a ranked Indic list
(`मेरा`, `मीरा`, …). Lookups fire **per keystroke** on the current word’s
prefix. The model is word-level and stateless; the client owns the roman buffer.

**Traffic shape (derived, not from a traffic dump):**

* One active typist ≈ 2–4 lookups/s while typing a word; most of a “session” is idle.
* ~500 concurrent *sessions* ≠ 500 QPS. Aggregate is bursty around hearing start.
* Prefix cache collapses retypes / backspaces to ~0.1 ms server-side.

**Where 100 ms goes:**

| Slice | Good LAN / localhost | Flaky court WAN |
|---|---|---|
| Network RTT | ~0–5 ms | **50–150 ms+** (can exhaust the budget alone) |
| Queueing | ~0 with one typist | grows if a shared box is overloaded |
| Model compute (warm, beam 4, MacBook) | **13–27 ms** | same compute, worse machine → 3–5× |

**What that rules out:** any design whose hot path crosses the courtroom WAN.
A dead or dark central API is exactly the failure we were hired to fix — so
central must not be required for suggestions.

---

## 2. Options considered and the choice

| Option | Verdict |
|---|---|
| Always-on central GPU API | Rejected — RTT + blackout + ~$380/mo per instance before redundancy |
| Shared central CPU API | Rejected — same RTT / blackout; compute was never the bottleneck |
| Per-session remote spin-up | Rejected — cold start + memory × sessions |
| Serverless inference | Rejected — cold start is a first-class keystroke failure |
| Court node as *primary* path | Rejected as primary — SPOF for power/LAN/maintenance; kept as mirror + weak-PC fallback |
| **Tier A local inference + central registry + progressive rescore** | **Chosen** |

**Chosen design in one line:** serve every keystroke from a warm IndicXlit
engine on the typist's machine; use central only to distribute signed,
versioned packages (mirrored once per court in production); ship `base`
first and upgrade to `rescore` in the background at a word boundary.

Provisioning is a **machine lifecycle** event, not login — a typist arriving
as the hearing starts cannot absorb a 138 MB download.

---

## 3. Prototype

Under `runtime/`:

* **C3 Central Model Registry** — atomic immutable publish, ETag polling, Range downloads, chaos knobs.
* **C2 Local Xlit Service** — package store, sync agent, dual engine slots, prefix LRU, localhost suggest API.
* **C1 Editor UI** — keystroke dropdown, commit/dismiss, status bar.
* **Tests** — 24 unit/e2e tests with a fake engine over real HTTP (no model needed).
* **Harnesses** — stock latency sweep, HTTP typing/load (3 layers), Dakshina-style quality gate, cost model.

Privacy: INFO logs redact roman / chosen text by default (`--log-content` to opt in).

---

## 4. Benchmarks

### 4.1 Engine compute (stock Hindi `XlitEngine`, MacBook)

From `results/latency_baseline.json` (beam 4, no rescore): warm p95 roughly
**10–30 ms** across short/medium words; cold start load+first infer ≈ **2.6 s**;
RSS ≈ **0.5 GB**. With rescore, RSS ≈ **1.7 GB**; first-run library download of
the all-language dict archive inflated an earlier “~69 s” cold number — loading
only the Hindi dict from a local package is ~2.3 s.

### 4.2 End-to-end service (localhost)

| Metric | Observed |
|---|---|
| Time to first suggestion (fresh + 138 MB pull) | ~4.7 s |
| Browser keystroke RTT + compute | 15–30 ms |
| Cached prefix | ~0.1 ms server |

HTTP harness (`benchmarks/latency_http.py`) records **client_ms / server_ms /
compute_ms** so transport vs queueing is visible. `--self-test` runs against an
in-process fake stack without torch; point `--url` at a warm local service for
real numbers.

**Pass criterion we hold ourselves to:** client-observed p95 **&lt; 100 ms** on
the weakest supported PC (hardware gate — see OPEN_QUESTIONS). MacBook numbers
are necessary but not sufficient.

### 4.3 Quality

Harness: `benchmarks/quality_dakshina.py` on `data/dakshina_hi_sample.tsv`
(exact string match top-1 / top-k). Arms: stock · packaged base · packaged rescore.

* Packaging gate: `base` candidate lists must equal `stock` on the same inputs.
* Absolute top-1 on the sample ≠ published Dakshina **60.56** (different split);
  full-split run is a next step.
* Courtroom NE spot-check: `data/courtroom_ne.tsv` (informational; Esc / keep-roman covers acronyms like `FIR`).

### 4.4 Cost model

`make bench-cost` → `results/cost_model.json`. Assumptions stated in the file
(CDN $/GB, VM, court count — estimates to verify).

| Scenario | ≈ $/mo @ 5k machines | ≈ $/1k users |
|---|---|---|
| Every machine pulls base+rescore | ~$240 | ~$48 |
| Weights-only (dict deduped) | ~$90 | ~$18 |
| **Court mirrors (~200 courts)** | **~$40** | **~$8** |
| 1× always-on GPU (rejected line) | ~$380 | ~$76 |

Keystroke volume does not appear in the central bill. Tier B boxes are capex
amortized where the hardware gate fails.

---

## 5. What breaks at 10× and what we would revisit

At ~50k users / ~5k peak sessions:

* Central poll load stays modest (mostly 304s); **egress grows with courts**, not typists — mirrors remain the lever.
* Tier B ops (multi-worker, TLS, on-site) dominate if many PCs fail the gate or IT forbids local installs.
* A bad model publish gets expensive — **signed manifests + cohort rollout + auto-rollback** move from “nice” to required.
* Latency levers if weak PCs miss the bar: beam 2, ONNX, int8 — each must re-pass the quality gate.

**Traded away in this window:** production auth, court-mirror implementation,
languages beyond Hindi, full Dakshina CI, OS-service packaging.

---

## 6. How to reproduce

```bash
cd runtime
make setup-runtime && make demo-check   # no model
# full path:
make setup && make warm-model && make publish
make central   # terminal 1
make local     # terminal 2 → http://127.0.0.1:8765/
```
