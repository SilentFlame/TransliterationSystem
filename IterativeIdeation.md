# IndicXlit Courtroom Runtime — Decision Log & Learning Notes

> **Shippable code + report:** see [`runtime/`](runtime/README.md).
> Open questions / next steps: [`runtime/docs/OPEN_QUESTIONS.md`](runtime/docs/OPEN_QUESTIONS.md).
> This file remains the chronological thinking trail from iterations 0–6.

## Iterations of thinking (what we considered, in order)

### Iteration 0 — Read the quality / API surface
- Read Aksharantar (model report), IndicXlit Hindi tables, Dakshina metric, `XlitEngine` beam/rescore API.
- Locked Hindi regression targets: Dakshina top-1 **60.56%**; Aksharantar native **55.59%** / NE **58.87%**.
- Metric that matches published numbers: **top-1 exact string match** (not Dakshina’s own CER/WER).
- Model: ~11M char Transformer; inference is **word-level**.

### Iteration 1 — Measure before architecture
- Stood up stock Hindi `XlitEngine` (Python 3.10 + torch 2.5.1).
- Latency baseline (`rescore=False` / `True`), beam ∈ {1, 4, 10}, short/medium/long words.
- **Findings:**
  - Warm compute p95 ≈ **15–50 ms** (no rescore), ≈ **12–61 ms** (rescore) — under 100 ms *compute*.
  - Cold start ≈ **2.6 s** without rescore; ≈ **69 s** with rescore (+ ~812 MB dict).
  - RSS ≈ **0.5 GB** vs ≈ **1.7 GB** with rescore.
- Built a typeahead playground: prefixes update live; quality improves as the roman word completes.

### Iteration 2 — First product / service sketch
1. Wrap the library as a transliteration service (accept request → top‑k).
2. Client UI types roman text, shows candidates; on space, pick selection or default top‑1.
3. Scale options considered:
   - **Option A:** N instances, one server per client, spin on demand.
   - **Option B:** Shared server; client holds the in‑flight roman prefix (`k` → `ki` → `kis`), clear on space.

### Iteration 3 — Pressure-test against the assignment
Assignment cares about: p95 &lt; 100 ms **end-to-end**, flaky courtroom links, cost ~$500/mo, no quality regression.

Gaps in Iteration 2:
- Remote RTT can burn the 100 ms budget even when the model is fast.
- Dead/flaky link = same failure as the dark hosted API.
- Per-client on-demand spin-up fights **cold start** and memory (0.5–1.7 GB × sessions).

What we kept from Iteration 2:
- Interaction model (prefix on client, ranked list, space commits).
- Stateless lookups: server/engine does not need intermediate-word session state.

### Iteration 4 — Chosen direction (hybrid)
**Infer locally; distribute centrally.**

- Client (or local sidecar) keeps a **static copy of weights** and runs suggestions on-device most of the time.
- Central service is for **bootstrap + versioned model updates**, not the per-keystroke hot path.
- When online: check version (pull on connect / periodic; event push optional) and refresh the local package.
- Default package bias: Hindi-only, `rescore=False` for footprint/latency; rescore optional if quality needs it.

### Iteration 5 — Progressive local service (non-rescore first, rescore when ready)
**Local service mirrors the central serving role; quality upgrades in the background.**

Idea:
- Boot a **local** transliteration service with the small package (`rescore=False`) and start answering queries immediately (~2.6 s cold, ~0.5 GB).
- **In parallel**, download the rescoring dictionary from central (version-pinned).
- On each query: if the dict is **not** ready → serve non-rescore; if download + load + warm-infer succeeded → serve rescore.
- Practically: two engine slots (A non-rescore, B rescore). `rescore` is an init-time concern in `XlitEngine`, not a per-request flip. Prefer **replace** A with B when ready to avoid holding ~0.5 + ~1.7 GB long-term.
- Gate “ready” on checksum/version **and** engine B loaded — file-on-disk alone is not enough.
- UX note: ranked lists can shift when rescoring engages; prefer switching between words / on idle, not mid-keystroke.

#### What this improved vs the problem statement
| Constraint | Before (Iter 4 bias) | After (Iter 5) |
|---|---|---|
| **p95 &lt; 100 ms** | Warm non-rescore already fine; forced rescore at boot risked a **~69 s** cold path before *any* suggestion. | Typists get warm lookups as soon as the light engine is up; rescore never blocks the hot path. |
| **Flaky / constrained courtroom links** | Local weights help, but bundling the big dict up-front makes first useful install heavier and more failure-prone. | Small package is enough to work; dict download can stall/fail and the service **keeps serving**. Offline after light boot still works. |
| **Quality (no material regression)** | Defaulting rescore **off** forever left quality on the table (natives often benefit from dict/LM). | Non-rescore is the durable baseline; rescore is a **progressive quality upgrade** when the court can finish the download — closer to stock IndicXlit-with-rescore without making it a startup requirement. |
| **Cost / footprint at scale** | Always-on rescore local copies → ~1.7 GB RSS everywhere; always-off → smaller but weaker quality story. | Pay ~0.5 GB to be useful; pay ~1.7 GB only where/when the upgrade completes. Avoids “everyone downloads the dict on day one.” |
| **“API went dark” / central role** | Central already limited to distribution. | Reinforces central as **artifact CDN** (weights + optional dict), not per-keystroke API — local service is the always-on copy of that serving surface. |

Net: Iteration 5 turns the Iter‑4 “rescore optional” line into a concrete **degraded-then-enhance** runtime: fast & offline-capable first, better ranked natives when bandwidth and RAM allow.

### Iteration 6 — What does “local” actually mean? (workstation vs court node)
**Tiered placement: workstation first, court node as package mirror + fallback for weak PCs.**

Iterations 4–5 said “run it locally” without pinning down *which* machine. Three readings were on the table:

1. **Per-workstation** — a copy on every PC that logs in.
2. **Per-courtroom / per-complex** — one shared server on the court LAN.
3. **Hybrid** — both, with defined roles.

#### Why “copy it when the user logs in” is the wrong trigger
A typist logging in at 10:30 as the hearing starts cannot wait for a 138 MB base package (let alone 358 MB of dict). **Provisioning is a machine-lifecycle event, not a session event**: the installer carries the base package, updates land in the background, and login only decides *who is typing*. This also makes the 500-concurrent number a property of machines, not of sessions.

#### What “500 concurrent” means under each reading
| Reading | What 500 concurrent actually stresses |
|---|---|
| Per-workstation | **Not a server number at all.** Each machine serves exactly one typist (~2–4 lookups/s). The questions become: (a) does the *weakest supported PC* hold p95 &lt; 100 ms — our 13–27 ms is MacBook-class, an old court PC could be 3–5× slower; (b) central sees ~500 polls/min ≈ **8 req/s of mostly 304s**; (c) release day is the real spike — 500 × 138 MB ≈ **69 GB**, plus ~180 GB if every machine also pulls the dict. |
| Per-court node | **A genuine capacity problem, sharded per court.** ~3 lookups/s per active typist × ~20 typists ≈ 60 lookups/s per complex. One engine ≈ 50 lookups/s at ~20 ms, and our engine serializes per process → **3–4 workers (~2 GB base / ~7 GB with dict)** to keep p95 safe at &lt;50% utilisation. LAN RTT (1–5 ms wired) is fine; Wi‑Fi jitter is not. |

#### The blackout argument (why option 2 alone fails)
“Courtroom blackout” is three distinct failures, and a court node only covers the first:

| Failure | Court node alone | Per-workstation copy |
|---|---|---|
| Internet / central down | Survives | Survives |
| Court node crashes / under maintenance | **Whole court loses suggestions** | Unaffected |
| Power cut / LAN switch failure | **Total outage** (needs UPS + on-site hands) | Survives on laptops/UPS’d PCs |

A shared node also stops being a localhost-only service: it needs network exposure, TLS, authN, and someone to maintain hardware in every court.

#### Decision: hybrid, with the workstation as the default
- **Tier A (default, hot path):** the Local Xlit Service on each typist PC, bound to localhost. Every keystroke is served here.
- **Tier B (supporting):** a court LAN node that is (a) a **package mirror** — the court’s internet link carries each release *once per court* instead of once per machine, and (b) an **inference fallback** for PCs that fail the hardware gate (low RAM / slow CPU / IT forbids a background service).
- **Client failover order:** localhost → court node (tight ~60 ms timeout) → **no suggestions, typing never blocks** (roman text stays, convert later).
- **No cloud API in the failover chain.** It cannot meet 100 ms over courtroom links and it is down during exactly the outages we care about. Emergency-only at best; nothing depends on it.

#### What this improved vs the problem statement
| Constraint | Before (Iter 5: “local”, unspecified) | After (Iter 6) |
|---|---|---|
| **p95 &lt; 100 ms** | True on a fast dev laptop; silent assumption about court hardware. | Hot path is explicitly localhost; the bar is now stated against the *weakest supported PC*, with a measured hardware gate deciding Tier A vs Tier B. |
| **Flaky links / blackout** | “Local weights survive the WAN” — but said nothing about node/power failure. | Each failure mode has a defined outcome; the single shared box is explicitly *not* the primary path. |
| **Scale** | 500 concurrent was ambiguous (sessions? QPS?). | Split into three measurable things: per-machine p95, central poll load (~8 req/s), and release-day egress. |
| **Cost** | Central = “artifact CDN”, unpriced. | Egress is the dominant central cost and the court mirror is the lever that cuts it ~25× (per-court instead of per-machine). |
| **Deployability** | Assumed we may install anywhere. | Government IT policy and PC specs become explicit inputs; Tier B is the answer when they say no. |

Net: Iteration 6 turns “local” into a **placement policy with a hardware gate and a failover chain**, and converts “500 concurrent” from one scary number into three separately measurable budgets.

Open inputs needed from the courts (these decide the Tier A/Tier B mix):
- Actual courtroom PC specs (RAM, CPU) and the resulting pass rate on the hardware gate.
- IT policy: may we install a background service at all?
- Typists per complex (sizes the court node), and whether courts have wired LAN + UPS.

---

## Decisions and why

| Decision | Choice | Why |
|---|---|---|
| Language for prototype | Hindi first | Assignment allows familiar start; published baselines exist. |
| Product UX | Suggestions on **prefix / keystroke** (optional debounce), not full-word-only | Matches IME feel; model still useful on prefixes; full-word-only feels laggy. |
| Prefix / buffer ownership | **Client-side** | Model is word-level &amp; stateless; avoids server session complexity. |
| Commit rule | User picks candidate, else **top‑1 on space** | Matches ranked-list editor behavior. |
| Hot-path placement | **Local weights / local process** | Beats RTT + survives flaky/offline courts; compute already fits 100 ms. |
| Central role | Model distribution + versioning (not every keystroke) | Fixes “API went dark” without paying RTT on every stroke. |
| Updates | Version check on connect (+ optional event notify) | Simple, enough for prototype; push is enhancement. |
| Rejected: per-client remote spin-up | — | Cold start (seconds–minute) and memory/cost don’t fit. |
| Rejected: remote-only shared API as primary | — | Fails flaky-link story; RTT risks p95. |
| Rescoring default | **Boot non-rescore; upgrade to rescore when dict ready** (Iter 5) | Avoids ~69 s / ~1.7 GB cold path; keeps suggestions available on flaky links; recovers dict quality without blocking p95. |
| Beam for interactive use | Start at **4** (library default); keep `beam ≥ topk` | Latency OK; topk cannot invent more candidates than beam kept. |
| Meaning of “local” | **Tier A workstation (default) + Tier B court node (mirror & weak-PC fallback)** (Iter 6) | Workstation keeps p95 and survives node/power failure; court node cuts release egress per court and covers PCs that fail the hardware gate. |
| Provisioning trigger | **Machine provisioning / background update, not user login** (Iter 6) | A login at hearing time cannot absorb a 138 MB download. |
| Failover chain | localhost → court node (~60 ms timeout) → **no suggestions, typing unblocked** (Iter 6) | Cloud fallback can’t meet 100 ms over court links and is down during the outages that matter. |

Still open (next work):
- Quality gate on Hindi Dakshina sample vs 60.56 / top‑k (non-rescore vs rescore once ready).
- Load harness: per-machine p95 on the **lowest-spec** PC, and shared court-node concurrency incl. hearing-start burst.
- Cost model + 10× narrative written against the tiered design (egress with and without court mirrors).
- Hardware gate: the actual RAM/CPU thresholds that decide Tier A vs Tier B, measured not assumed.
- Court-node hardening if Tier B is widely needed: multi-worker inference, TLS/authN, UPS, second node.
- Privacy: stop logging roman input / chosen text at INFO before any real courtroom pilot.

---

## ML / AI terms to read deeper

Grouped by how they show up in *this* problem.

### Model & task
- **Transliteration vs translation** — script/phonetic mapping, not meaning.
- **Romanization / back-transliteration** — Latin → native (what we serve).
- **Grapheme / phoneme** — character vs sound; explains vowel confusions in IndicXlit errors.
- **Named-entity transliteration** — names behave differently; Aksharantar NE slice; rescoring helps less.
- **Multilingual / multi-script models** — one model, language tag token (IndicXlit design).

### Architecture (IndicXlit internals)
- **Sequence-to-sequence (seq2seq)** — roman char sequence → Indic char sequence.
- **Encoder–decoder Transformer** — 6+6 layers, d=256, 4 heads, FFN 1024 (~11M params).
- **Character-level modeling** — vocabulary is characters, not subwords/BPE (for this model).
- **Autoregressive decoding** — output grows one token at a time; latency ∝ output length × beam work.
- **Cross-attention / self-attention** (high level) — how decoder conditions on the roman input.
- **Embedding dimension, attention heads, FFN size, parameter count** — how “small” 11M is vs LLMs.

### Inference & serving knobs
- **Beam search / beam width** — explore multiple hypotheses; quality–latency tradeoff.
- **Top‑k candidates** — what the UI shows; product metric ≠ only top‑1.
- **Greedy decoding** — beam=1 special case.
- **Rescoring / reranking** — combine model score with unigram LM / dictionary (`α·T + (1−α)·P`).
- **Language model (unigram LM)** — frequency prior over native words.
- **Cold start vs warm inference** — load/weights vs steady-state latency.
- **Batching** — less relevant for interactive single-word keystrokes; more for offline eval.
- **Quantization / ONNX / compilation** — later latency or size knobs if local CPU is tight.

### Evaluation
- **Exact-match accuracy (top‑1 / top‑k)** — IndicXlit / Aksharantar primary reporting.
- **CER / WER** — Dakshina paper’s own metrics; don’t mix when citing 60.56.
- **Lexicon / attestation** — multiple valid roman↔native pairs; gold set for a roman form.
- **Train/test leakage / word overlap** — why Aksharantar filtered training against Dakshina test.
- **Regression baseline** — don’t fall materially below published stock engine on same metric.

### Systems / MLOps (runtime side of the assignment)
- **On-device / edge inference** — local weights; privacy/latency/offline.
- **Model registry / versioning** — central package + version manifest.
- **Model distribution / OTA update** — pull-on-connect vs event-driven push.
- **p50 / p95 / p99 latency** — tail latency is what typists feel.
- **RTT vs compute budget** — split the 100 ms.
- **RSS / memory footprint** — especially with rescore dictionaries.
- **Stateless serving** — no per-keystroke server session for the current word.
- **Debounce** — client-side request coalescing under fast typing.
- **QPS / concurrency vs DAU** — 500 sessions ≠ 500 QPS; derive from typing behavior.

### Data (context, not training for this assignment)
- **Parallel transliteration corpus** — Aksharantar mining (Wikidata, Samanantar, IndicCorp, manual).
- **Mining vs human annotation** — noise vs diversity; why manual data helped rare/native words.

### Suggested short reading order
1. Beam search + autoregressive decoding (intuition for our latency table).
2. Exact-match vs CER/WER (so quality numbers stay honest).
3. Rescoring / shallow fusion style reranking (why dict helps natives, not names).
4. On-device inference + model versioning (your chosen architecture).
5. Transformer encoder–decoder at a high level (what the 11M model *is*).

---

## One-line architecture summary

**Local IndicXlit for every keystroke (non-rescore first, rescore when the dict finishes downloading); client holds the roman prefix; central ships versioned weights + optional dict when the court is online.**
