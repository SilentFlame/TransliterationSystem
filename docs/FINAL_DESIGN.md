# Final Design — IndicXlit Courtroom Transliteration Runtime

Decision record for the design we are building to. Explains **what we ship, where it runs, how it is distributed, and how we will prove it** — plus the options we rejected and why.

Related: [HLD](HLD.md) · [LLD](LLD.md) · [decision log](../NOTES_decisions_and_learning.md) · [README](../README.md)

Status legend used throughout: **[built]** works today in this repo · **[planned]** designed, not yet implemented.

---

## 1. The decision in one page

> Serve every keystroke from a **warm IndicXlit engine on the typist's own machine**. Use the central service only to **distribute signed, versioned model packages**, mirrored **once per court** rather than once per machine. Ship a usable base engine first and upgrade quality in the background.

Four choices carry the design:

| # | Decision | Why it is the load-bearing one |
|---|---|---|
| 1 | **Inference is local to the typist machine** (Tier A) | The 100 ms bar is end-to-end. Courtroom WAN RTT (50–150 ms, flaky) can blow the budget on its own, and a dead link is the exact failure we were hired to fix. |
| 2 | **Central is a model registry, not an inference API** | Central cost and capacity become independent of keystroke volume. "The API went dark" stops being an outage and becomes "updates are paused". |
| 3 | **Two quality tiers, progressive** | Base package (138 MB) makes a machine useful in seconds; the 358 MB rescore dictionary upgrades quality later without ever blocking the hot path. |
| 4 | **Court LAN node is a mirror + weak-PC fallback, not the primary path** | Cuts release-day egress ~25× per court, covers PCs that fail the hardware gate, but never becomes a single point of failure for a whole courtroom. |

---

## 2. Final model design — what exactly runs

**Model:** AI4Bharat IndicXlit `en→indic` transformer, used as-is. ~11M parameters, character-level, encoder–decoder (6+6 layers, d=256, 4 heads, FFN 1024), autoregressive, **word-level** inference. No retraining, no architecture change.

**Decoding, fixed for interactive use:**

| Knob | Value | Reason |
|---|---|---|
| Beam width | 4 (library default) | Measured latency is comfortable; `topk` can never exceed the beam, so 4 is the floor for a 4-item list. |
| `topk` shown | 4 candidates + "keep roman" row | Matches the ranked-list editor behaviour; 5th row lets the typist keep `FIR`, names, English. |
| Language | Hindi only in the package | Keeps the package at 138 MB instead of carrying 21 languages. Manifests are per-language, so adding one is a packaging job, not a redesign. |
| Rescoring | **Tier 2**, loaded when the dictionary is present | Quality upgrade; never a boot requirement. |

**The two engine tiers [built]:**

| Tier | Contents | Disk | RSS | Load time | Role |
|---|---|---|---|---|---|
| `base` | weights + vocab | 138 MB | ~0.5 GB | ~2.6–4.2 s | The durable baseline. Enough to be useful offline. |
| `rescore` | + Hindi unigram dictionary | +358 MB | ~1.7 GB | ~2.3 s from local disk | Reranks candidates (`α·T + (1−α)·P`, α=0.9). Better on native words. |

**Progressive upgrade rule [built]:** boot `base`, download `rescore` in the background, load it into a **staged** slot, and swap it in only at a **word boundary** (next word start, a commit, or ~2 s idle) so the ranked list never reshuffles under the typist's fingers. A package counts as ready only when every file passes sha256 *and* the engine has loaded and warmed — a file on disk is not enough.

**Deliberately unchanged:** the inference code path is the library's own. We subclass only to point `models_path` at our versioned package directory. That is what lets us claim "no quality regression" and then prove it with the same scorer against stock.

**Quality levers held in reserve** (only if the hardware gate forces them): beam 2, ONNX export, int8 quantisation, a trimmed frequency dictionary. Each must re-run the Dakshina harness; if top-k drops materially, revert.

---

## 3. Where it sits — tiered placement

### Tier A — typist workstation (default, hot path) [built]

The Local Xlit Service runs on the PC, bound to `127.0.0.1`, serving the editor in the browser. One service per typist. Suggestions are local, so they survive WAN loss, central outage, and court-server maintenance.

### Tier B — court LAN node (supporting) [planned]

The *same service*, deployed on a court-complex box, with two jobs:

1. **Package mirror** — workstations sync from it, so the court's uplink carries each release once per court.
2. **Inference fallback** — serves PCs that fail the hardware gate (low RAM, slow CPU, or IT forbids a background service).

It is deliberately **not** the primary path: one box serving a whole court is a single point of failure for power, LAN, and maintenance.

### The hardware gate [planned]

A machine qualifies for Tier A if it passes a measured check at install time:

* available RAM ≥ ~1 GB for `base` (≥ ~2.5 GB before `rescore` is allowed — already enforced by `--rescore auto`), and
* a short benchmark of representative words meets a **p95 compute budget of ~60 ms** (leaving headroom under the 100 ms end-to-end bar).

Pass → Tier A. Fail → Tier B client (editor points at the court node). The thresholds above are the *starting* proposal; the real numbers come from benchmarking actual court hardware.

### Failover chain [planned]

```
localhost (Tier A)  →  court node (Tier B, ~60 ms timeout)  →  no suggestions
```

The third state is a first-class design state, not a crash: the dropdown shows why, **typing is never blocked**, the roman text stays, and it can be converted later. **No cloud inference in this chain** — it cannot meet 100 ms over court links and is unavailable during exactly the outages we care about.

### What "500 concurrent" means here

It is not one number. It is three budgets, measured separately:

| Budget | Where it lands | Rough expectation |
|---|---|---|
| Per-typist latency | One workstation, one typist, ~2–4 lookups/s | p95 < 100 ms end-to-end on the **weakest supported PC** |
| Central request load | 500 machines polling the registry | ~8 req/s, almost all `304 Not Modified` |
| Release-day egress | Central → courts | The real spike; see §5.3 |

---

## 4. How it is distributed

**Unit of distribution [built]:** an immutable, versioned package set per language, described by a manifest (schema 1) listing every file with size and sha256. `base` is mandatory; `rescore` is optional.

**Guarantees in place today [built]:**

| Property | Mechanism |
|---|---|
| Atomic publish | Stage → write manifest last → `os.replace` the directory → swap `LATEST`. Readers never see a partial version. |
| Immutability | Re-publishing an existing version id is rejected. Rollback = point `LATEST` back. |
| Resumable downloads | HTTP `Range` into a `.part` file; survives a dropped court link. |
| Integrity | sha256 verified before the file is moved to its final path; corrupt partials are deleted and refetched. |
| Cheap polling | ETag on `latest`; unchanged means a `304` with no body. |
| Delta-ish updates | Files identical across versions (same path + sha256) are hard-linked locally — a weights-only release does **not** re-download the 358 MB dictionary. |
| Offline-first boot | The service serves from the local package store *before* contacting central. |
| Safe upgrades | Never downgrade: a client on `rescore` waits for the new version's `rescore` rather than stepping down to `base`. |

**Provisioning trigger:** machine lifecycle, **not** user login. The installer carries `base`; updates land in the background. A typist logging in as the hearing starts cannot absorb a 138 MB download.

**Planned additions:**

| Gap | Plan |
|---|---|
| Authenticity | **Signed manifests** (ed25519), public key pinned in the client. sha256 catches corruption, not a malicious publisher. |
| Blast radius | **Cohort rollout** — `LATEST` per channel / percentage of client ids, driven by heartbeat health, with automatic rollback thresholds. |
| Egress | **Court mirror** as a caching proxy; staggered rollout windows so a court's uplink is not hit by every PC at once. |
| Transport | TLS everywhere; Tier B additionally needs authN since it is no longer localhost-only. |

---

## 5. How we benchmark it

Three separate exercises, each with its pass criterion stated up front. **All three are [planned]** — only the single-process warm-compute sweep (`benchmarks/latency_baseline.py`) exists today.

### 5.1 Quality — the Dakshina gate

**Question:** did our packaging and runtime change the suggestions at all, and are we near the published baseline?

* **Data:** Hindi Dakshina romanization lexicon (roman → Devanagari), a fixed documented sample, plus a small courtroom named-entity set (names, places, `FIR`-style tokens).
* **Metric:** top-1 and top-4 **exact string match** — the same metric AI4Bharat reports. Do *not* mix in Dakshina's own CER/WER.
* **Arms:** stock `XlitEngine` · our packaged `base` · our packaged `rescore`.
* **Pass criteria:**
  1. **Packaged `base` ≡ stock non-rescore** on the same inputs — identical candidates expected, since the inference path is untouched. Any diff is a packaging bug.
  2. Reference point: IndicXlit's published Hindi Dakshina top-1 is **60.56**. Our sample may differ; the deployment gate is the stock-vs-ours delta, not the absolute number.
  3. `rescore` must not be *worse* than `base` on top-1; if it is, the upgrade policy is wrong.
* This file becomes the regression gate for every later optimisation (beam reduction, ONNX, quantisation) and a **pre-publish gate** in the release pipeline.

### 5.2 Latency and load

Today's `benchmarks/latency_baseline.py` measures single-process warm compute [built]. What is missing is a harness driving the real HTTP endpoint under realistic typing.

**Always record three layers** so queueing is visible, not inferred:

| Layer | Source | Tells you |
|---|---|---|
| Client-observed | harness stopwatch | what the typist feels — **this is the p95 that counts** |
| `server_ms` | `/v1/suggest` response | API + routing overhead |
| `compute_ms` | `/v1/suggest` response | the engine alone |

Derived: `client − server_ms` = transport; `server_ms − compute_ms` = queueing/cache.

**Scenarios:**

| Scenario | Shape | What it proves |
|---|---|---|
| Single typist, cold cache | distinct words, ~3 lookups/s, 5 min | the honest per-keystroke p95 |
| Single typist, realistic cache | prefix walks (`a`→`ad`→`adal`…), backspaces, retypes | the hit rate we actually get (~0.1 ms on hit) |
| **Weakest supported PC** | same as above on target court hardware | **the gate that decides Tier A vs Tier B** |
| Court node, steady | ~20 typists × ~3 lookups/s ≈ 60 lookups/s | worker count needed for Tier B |
| Court node, hearing-start burst | all clients fire within 1–2 s, then settle | queue depth and **recovery time**, not just p95 |
| Central registry | 500 clients polling + N downloading | that polling is trivial and downloads do not starve it |
| Chaos / offline | kill central mid-run; `--chaos-fail-rate` | local p95 unchanged; downloads resume |

**Report per run:** requests, failures, achieved RPS, p50/p95/p99 for all three layers, CPU, RSS, cache hit rate, active version/tier — plus **cold start** and **time-to-first-suggestion** separately (measured today: ~4.7 s fresh install including the 138 MB pull).

**Pass criterion:** p95 **client-observed** < 100 ms for Tier A on the weakest supported PC, cold cache. For bursts, also state time-to-recover.

**Honest caveat:** our engine serializes inference per process (fairseq generator safety). That is correct for Tier A — one typist, one service. It means a Tier B court node needs **multiple worker processes**, and that blasting 500 concurrent requests at a single local service measures overload behaviour, not the production topology. The meaningful 500-user figure is 500 independent Tier A services, i.e. a per-machine profile multiplied out.

**Known risk this will expose:** our 13–27 ms compute is MacBook-class. A 3–5× slower court PC lands at ~40–135 ms — straddling the bar. Expect the hardware gate to matter, and keep the reserve levers (beam 2, ONNX, int8) ready.

### 5.3 Cost model

Central cost is **storage + egress + one small VM** and is independent of keystroke volume. Egress dominates, and the court mirror is the lever.

Assumptions to state explicitly (prices are *estimates to verify*, not quotes):

```
machines              5,000
base package          138 MB      (measured)
rescore package       358 MB      (measured)
release cadence       1 / month
CDN egress            ~$0.085/GB  (assumption)
central VM            ~$30/month  (assumption)
object storage        ~1.5 GB retained  → negligible
```

| Scenario | Egress per release | ≈ $/month | ≈ $/1,000 users/month |
|---|---|---|---|
| Every machine pulls direct (base + rescore) | 5,000 × 0.496 GB ≈ **2.5 TB** | ~$240 | ~$48 |
| Weights-only release (dict reused via sha256 dedupe) | 5,000 × 0.138 GB ≈ **690 GB** | ~$90 | ~$18 |
| **With court mirrors (~200 courts)** | 200 × 0.496 GB ≈ **99 GB** | ~**$40** | ~**$8** |

Comparison line to publish alongside: a single always-on GPU instance (g4dn.xlarge class) is **~$380/month** before redundancy — one instance already consumes most of the $500 ceiling, cannot serve 500 concurrent typists with headroom, **and does not fix latency or the blackout problem**. That is the option to price and reject, not the default.

Also model: Tier B hardware (a modest box per court where needed) as **capex per court**, and bandwidth cost at 10× users with and without mirrors.

---

## 6. Alternatives considered and rejected

| Option | Why rejected |
|---|---|
| **Always-on central GPU API** | WAN RTT alone can exceed the 100 ms budget; a dead link reproduces the outage we are fixing; ~$380/mo per instance before redundancy, scaling with every court onboarded. |
| **Central CPU API (shared)** | Same RTT and same blackout exposure. Compute was never the problem. |
| **Per-client remote spin-up** (one server per session) | Cold start of seconds-to-a-minute at hearing time; 0.5–1.7 GB × sessions of memory; cost scales with users. |
| **Serverless inference** | Cold start is a first-class latency failure for a per-keystroke workload. |
| **Court node as the *primary* inference path** | A single box per court is a single point of failure for power, LAN, and maintenance; needs TLS/authN/multi-worker and on-site hands. Kept as mirror + fallback instead. |
| **Download the model on user login** | A login at hearing time cannot absorb 138 MB. Moved to machine provisioning + background update. |
| **Ship `rescore` as a boot requirement** | Blocks the first suggestion behind a 358 MB download and ~1.7 GB RSS; fragile on constrained links. Made a background upgrade instead. |
| **Suggest only on word boundary** (not per keystroke) | Lower QPS, but it does not feel like an IME. Keystroke + prefix cache is affordable precisely because inference is local. |
| **Server-side prefix/session state** | The model is word-level and stateless; the client owning the roman prefix removes a whole class of session bugs. |
| **Cloud API in the failover chain** | Cannot meet 100 ms over court links and is down during the outages that matter. Emergency-only at best. |
| **Retraining / distillation for speed** | Out of scope and unnecessary until the hardware gate proves stock inference is too slow. Quantisation/ONNX is the cheaper first lever. |

---

## 7. What exists vs what is next

**Built and verified in this repo:** central registry with atomic immutable publishing; manifest schema with path/hash validation; local service with prefix cache, dual engine slots and word-boundary promotion; resumable, checksum-verified downloads with local dedupe and pruning; offline-first boot and backoff; keystroke editor UI with dropdown, accept/pick/keep; JSON logging on both sides; 24 tests.

**Built in this `runtime/` tree (beyond the prototype):** Dakshina-style quality harness + checked-in sample; HTTP typing/load harness with three latency layers; cost model script with GPU comparison line; privacy-safe INFO logs by default (`--log-content` to opt in); `requirements-runtime.txt` for model-free verification.

**Next, in priority order:**

1. **Full Dakshina split + publish gate** — run the quality harness on the official test lexicon and refuse to move `LATEST` on failure.
2. **Weakest-PC profile** (§5.2) — this decides the hardware gate thresholds, and therefore how much Tier B we need.
3. **Signed manifests + cohort rollout with automatic rollback.**
4. **Tier B node**: multi-worker inference, TLS/authN, mirror mode, client failover chain.
5. **Dependency hygiene** — replace the remaining `--no-deps` ML freeze with a minimal self-consistent lockfile.
6. See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for interpretations still open with reviewers.
