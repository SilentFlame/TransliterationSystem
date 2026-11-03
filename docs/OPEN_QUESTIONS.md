# Open questions, interpretations, and next steps

Companion to [FINAL_DESIGN.md](FINAL_DESIGN.md). Things we deliberately left
open, contested, or sequenced after the prototype — plus what we would do next
with more time. Useful for discussion with reviewers and for a pilot team.

Status: **open** = needs a real input or a decision; **proposed** = our current
lean; **next** = concrete follow-up work.

---

## 1. Open for discussion / interpretation

### 1.1 What does “500 concurrent” bind?

**Open.** Three readings are all defensible; we measured them separately rather
than collapsing them into one QPS number:

| Reading | What to measure |
|---|---|
| 500 Tier A workstations | per-machine p95 on the *weakest* PC |
| 500 clients polling central | registry RPS (mostly 304) |
| 500 typists on Tier B nodes | multi-worker capacity per court |

**Proposed:** treat the assignment’s “500 concurrent” as **500 independent
Tier A services**, and report the other two as supporting budgets. A single
local process blasted with 500 concurrent requests measures overload of the
wrong topology.

### 1.2 Hardware gate thresholds

**Open — needs court PC data.** Starting proposal from FINAL_DESIGN:

* RAM ≥ ~1 GB free for `base`; ≥ ~2.5 GB before `rescore` (`--rescore auto` already gates on total RAM ≥ 4 GB).
* Install-time microbench: p95 compute ≤ ~60 ms on a fixed word list.

**Interpretation risk:** our MacBook compute (13–27 ms) may become 40–135 ms on
older courtroom PCs. That straddles the 100 ms end-to-end bar and decides how
large Tier B must be.

**Next:** run `benchmarks/latency_http.py` + the microbench on a sample of real
court hardware; publish pass rate before sizing Tier B boxes.

### 1.3 IT policy: may we install a background service?

**Open.** If the answer is “no” for a large fraction of courts, Tier B stops
being a niche fallback and becomes the default path — which changes ops
(TLS, authN, multi-worker, UPS, on-site hands) and cost (capex per court).

**Proposed:** keep Tier A as the product default in the design narrative;
treat a high “no local service” rate as a pilot finding that triggers Tier B
hardening, not as a reason to put cloud inference back on the hot path.

### 1.4 Quality: sample vs full Dakshina

**Open interpretation of the absolute number.** Published Hindi Dakshina top-1
is **60.56**. Our checked-in `data/dakshina_hi_sample.tsv` is a curated sample
for offline reproducibility — its absolute top-1 is **not** comparable to 60.56.

**Proposed gate (what we implement):**

1. Packaged `base` ≡ stock candidates on the same inputs (packaging bug detector).
2. `rescore` top-1 ≥ `base` top-1 on the same file.
3. Optional: full Dakshina test split as a pre-publish CI job when bandwidth allows.

### 1.5 Rescore as default upgrade vs opt-in

**Proposed:** progressive upgrade (`base` first, `rescore` when ready and RAM
allows). Alternative reading: some courts may prefer never to pay 1.7 GB RSS.
`--rescore off` already supports that; the open question is the *fleet default*
after the hardware gate is measured.

### 1.6 Failover third state (“no suggestions”)

**Proposed:** typing must never block; roman stays; convert later. Some product
stakeholders may want a cloud emergency API. We reject that on the hot path
(cannot meet 100 ms over court links; down during the outages that matter) but
leave “emergency-only offline batch convert” as a separate product surface.

### 1.7 Privacy vs debuggability

**Proposed (implemented in this tree):** redact roman / chosen from INFO by
default; `--log-content` for local debugging. Open: how long to retain even
redacted logs in a real court deployment, and whether heartbeats may carry
prefixes at all (today they do not).

### 1.8 Cost assumptions

CDN $/GB, VM size, and court count are **estimates to verify**. The model’s
shape (egress dominates; mirrors cut ~25×; GPU line loses on latency *and*
cost) is the claim; the exact dollars are not.

---

## 2. Next steps (priority order)

From FINAL_DESIGN §7, refined after this shippable pass:

| # | Work | Why |
|---|---|---|
| 1 | Run quality gate on **full** Dakshina hi split + package identity check in CI | Absolute number + pre-publish gate |
| 2 | Weakest-PC profile + finalize hardware-gate numbers | Decides Tier A/B mix |
| 3 | Wire `bench-quality` into `publish` (refuse to move `LATEST` on gate fail) | Release safety |
| 4 | Court mirror (caching proxy) + staggered rollout windows | Egress + blast radius |
| 5 | Signed manifests (ed25519) + cohort `LATEST` + auto-rollback from heartbeats | Authenticity + safe fleet updates |
| 6 | Tier B multi-worker + TLS/authN + client failover (`localhost → court → none`) | Weak-PC / no-install courts |
| 7 | Dependency hygiene — replace the `--no-deps` freeze with a minimal self-consistent lock | Installability on reviewer machines |
| 8 | ONNX / int8 / beam-2 only if the hardware gate fails on stock | Quality harness must re-pass |

---

## 3. What we would revisit with more time

* **Prefix debounce** on the UI if weak PCs queue under fast typists (product feel vs load).
* **Per-language packages beyond Hindi** — manifests already key by `lang`; packaging work, not redesign.
* **Tray / OS-service packaging** of the local process for non-technical installs.
* **Metrics export** (Prometheus) instead of grepping JSON logs.
* **Named-entity dedicated handling** — courtroom names often want “keep roman”; UI already supports Esc / last-row keep.

---

## 4. Explicit non-goals (for this assignment window)

Retraining, distillation, 21-language packaging, a production editor product,
multi-region active-active central, and putting GPU inference back on the
keystroke path.
