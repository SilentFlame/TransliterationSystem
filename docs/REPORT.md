# Final Report — IndicXlit Courtroom Transliteration Runtime

**Assignment:** bring back live Indic transliteration for courtroom typists after the hosted
AI4Bharat API went dark, and run it in production at p95 < 100 ms per lookup for ~5,000 daily
users (~500 concurrent), on flaky courtroom links, without regressing quality, at roughly
$500/month.

**Answer in one paragraph:** run the IndicXlit model on the typist's own machine for every
keystroke, and use the cloud only to ship versioned model packages. On this MacBook a keystroke
costs 15–40 ms of model time, and the internet is never on that path. When the court link goes
down, suggestions keep working. Central's bill depends on releases, not keystrokes: about
$237/month for what is built today, and about $39/month once each court mirrors the package. A
16-typist stress test met the latency bar on a shared machine, and pinning PyTorch to one thread
per process removed every lookup over 100 ms.

| Read next | For |
|---|---|
| [FINAL_DESIGN.md](FINAL_DESIGN.md) | Full decision record |
| [HLD.md](HLD.md) · [LLD.md](LLD.md) | Components and module contracts |
| [../results/results_summary.md](../results/results_summary.md) | Every measured number, by file |
| [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) | Open interpretations and next steps |
| [../IterativeIdeation.md](../IterativeIdeation.md) | How the design evolved, iteration by iteration |
| [../README.md](../README.md) | Setup and commands |

All numbers below come from JSON files in `results/`, measured on an Apple M3 Pro (11 cores,
18 GB RAM). They are fast-hardware numbers; section 9 covers what changes on a weak courtroom PC.

---

## 1. The workload, and what 100 ms actually demands

Typists type romanized Hindi (`mera`) and pick from a ranked list in Devanagari
(`मेरा`, `मीरा`, …). The model works one word at a time and keeps no state, so the editor holds
the roman text and asks for suggestions on the current prefix after every keystroke.

**Traffic shape**

* One active typist sends about 2–4 lookups per second while typing a word. Most of a session
  is idle (listening, reading, waiting for the next speaker).
* 500 concurrent sessions is not 500 requests per second. Load is bursty, and the burst is the
  start of a hearing, when everyone begins at once.
* Backspaces and retyped prefixes repeat earlier lookups, so a prefix cache answers them in
  about 0.1 ms.

**Where the 100 ms goes**

| Slice | Typist's own machine | Across a courtroom WAN |
|---|---|---|
| Network round trip | ~0–5 ms | 50–150 ms or more; can spend the whole budget alone |
| Queueing | ~0 with one typist | grows when a shared box is busy |
| Model compute (warm, beam 4) | 15–40 ms on this MacBook | same model; a slower PC adds 3–5× |

**What that rules out:** anything that crosses the courtroom WAN on every keystroke. A dead
central API is the failure we were asked to fix, so suggestions cannot depend on central being
reachable.

---

## 2. How the design evolved

The full trail is in [IterativeIdeation.md](../IterativeIdeation.md). Summary:

| Iteration | What we did or thought | What changed |
|---|---|---|
| 0 | Read the model report, Dakshina metric, and `XlitEngine` API | Fixed the quality target (Hindi Dakshina top-1 60.56) and the metric (exact string match, top-k) |
| 1 | Measured the stock engine before any architecture | Compute is already under 100 ms; rescore costs ~1.7 GB RAM |
| 2 | Sketched a transliteration server with a client-held prefix | Kept the interaction model; questioned where the server runs |
| 3 | Pressure-tested against the assignment | Remote round trip and dead links break a shared remote API |
| 4 | **Infer locally, distribute centrally** | Central becomes a model registry, not an inference API |
| 5 | Progressive engines: small model first, rescore dictionary later | First suggestion never waits on the 358 MB dictionary |
| 6 | Pinned down what "local" means | Typist PC is the default; a court LAN node is a mirror and fallback; installs happen at machine setup, not at login |

---

## 3. Options considered

| Option | Verdict | Reason |
|---|---|---|
| Always-on central GPU API | Rejected | WAN round trip can exceed 100 ms; a dead link reproduces the outage; ~$380/month per instance before redundancy |
| Shared central CPU API | Rejected | Same round-trip and blackout problems; compute was never the bottleneck |
| One remote server spun up per session | Rejected | Seconds-to-minutes cold start at hearing time; 0.5–1.7 GB memory per session |
| Serverless inference | Rejected | A cold start is a visible keystroke failure |
| Court LAN node as the main path | Rejected as primary | One box per court fails together on power, network, or maintenance; kept as mirror + fallback |
| Download the model at user login | Rejected | A typist logging in as the hearing starts cannot wait for 138 MB |
| Require the rescore dictionary at startup | Rejected | Blocks the first suggestion behind 358 MB and ~1.7 GB RAM |
| Suggest only after a full word | Rejected | Fewer lookups, but does not feel like typing assistance |
| **Local engine per typist + central registry + progressive rescore** | **Chosen** | Keystroke path never leaves the machine; central cost is releases, not keystrokes |

---

## 4. What we built

Three running components and their tests, all in this repository.

| Component | Where | What it does |
|---|---|---|
| **C1 Editor UI** | `xlit/local/static/` | Browser editor. Dropdown on every keystroke; Space or punctuation accepts; arrows, 1–9, or click pick another; Esc keeps roman (for `FIR`, names). Status bar shows version, tier, link state, download progress, last latency. |
| **C2 Local Xlit Service** | `xlit/local/` | Runs on the typist's machine on `127.0.0.1:8765`. Serves suggestions from a warm engine with a prefix cache. Background sync downloads, verifies, and stages new packages. Holds an active and a staged engine and swaps only at a word boundary so the list never reshuffles mid-word. |
| **C3 Central Model Registry** | `xlit/central/` | Publishes immutable, versioned packages per language. Clients poll with ETag (304 when nothing changed) and download with HTTP Range so a dropped link resumes. Includes chaos switches for a flaky uplink. |

**Behaviour that is built and tested**

* **Atomic publish:** a new version appears all at once or not at all; a version number cannot
  be reused; rollback is pointing `LATEST` back.
* **Integrity:** every file is checked by sha256 before use; a corrupt partial download is
  deleted and fetched again.
* **Resumable downloads** over a dropped link.
* **Offline-first boot:** the service serves from its local copy before talking to central.
* **Progressive upgrade:** small `base` engine first; `rescore` dictionary downloads in the
  background and takes over at the next word boundary.
* **Cheap updates:** files unchanged between versions are reused locally, so a weights-only
  release does not re-download the 358 MB dictionary.
* **Privacy by default:** logs do not contain the typed roman text or chosen words unless
  `--log-content` is passed.
* **29 tests** over real HTTP with a fake engine (no model download needed): publish rules,
  manifest validation, word-boundary promotion, offline boot, resume and corruption rejection,
  API validation, log redaction.

**Measurement tools built**

| Tool | Measures |
|---|---|
| `benchmarks/latency_baseline.py` | Raw model speed by beam width and word length |
| `benchmarks/latency_http.py` | One typist through the real service; records client, server, and compute time separately |
| `benchmarks/quality_dakshina.py` | Exact-match quality: stock vs our package vs rescore |
| `benchmarks/cost_model.py` | Monthly cost and cost per 1,000 users, with assumptions in the output |
| `benchmarks/wan_proxy.py` | A courtroom WAN link between services and central: delay, jitter, shared bandwidth, drops, and a kill switch |
| `benchmarks/fleet_stress.py` | N real services started at once, then N typists replaying sentences keystroke by keystroke |

---

## 5. What we designed but did not build

These are part of the design, with a defined role, but are not in this prototype.

| Piece | Role | Why it matters |
|---|---|---|
| **Court LAN mirror** (Tier B) | Each court downloads a release once and serves it to its PCs | Cuts release-day traffic about 25× and is the $39/month cost line. The stress test below shows why: 16 PCs on one uplink waited three minutes. |
| **Court fallback server** (Tier B) | Same service with several workers, for PCs too weak to run the model or where IT forbids a local service | Covers the machines that fail the hardware check |
| **Hardware check at install** | RAM and a short speed test decide whether a PC runs the model itself or uses the court server | The real threshold depends on actual courtroom PCs |
| **Failover order** | Typist's machine → court server (short timeout) → no suggestions, typing never blocks | Cloud inference is deliberately absent: it cannot meet 100 ms over a court link and is down when it matters |
| **Signed packages and staged rollout** | Publisher signs manifests; new versions go to a slice of machines first, with automatic rollback | sha256 catches corruption, not a malicious publisher; staging limits a bad release |

---

## 6. Benchmarks — single machine, single typist

### 6.1 Model speed

`results/latency_baseline.json` (no rescore), 60 calls per cell:

| Beam | Short word p95 | Medium word p95 | Long word p95 |
|---|---|---|---|
| 1 | 10.6 ms | 18.6 ms | 25.5 ms |
| **4 (used)** | **15.3 ms** | **30.3 ms** | **40.1 ms** |
| 10 | 15.6 ms | 37.0 ms | 48.8 ms |

Model startup is about 2.6 s and uses about 0.5 GB of memory. With the rescore dictionary,
warm speed is similar (beam-4 p95 14.8 / 31.8 / 42.9 ms) but memory grows to about 1.7 GB.
The 68.9 s rescore startup in that file includes a first-time download of the library's
all-language dictionary; loading the Hindi dictionary from our package takes about 2.3 s.

### 6.2 One typist through the real service

`results/latency_http.json`, warm local service, zero failures:

| Scenario | What it simulates | Client p95 |
|---|---|---|
| New words | 20 distinct words at 3 lookups/s | 40.3 ms |
| Letter by letter | `a` → `ad` → `ada` …, then a retype (57 of 143 cached) | 23.3 ms |
| Burst | 20 lookups at once, all cached | 6.1 ms |

The browser-to-service hop is under 3 ms; almost all the time is the model. A fresh machine
gets its first suggestion about 4.7 s after start, including the 138 MB download from a local
central.

---

## 7. Quality

### 7.1 Did packaging change the model?

`results/quality_dakshina.json`, beam 4, top 4, exact string match:

| Engine | 405-word Hindi sample top-1 | top-4 | 49 courtroom names top-1 |
|---|---|---|---|
| Original library | 78.77% | 94.07% | 75.51% |
| Our package, base | 78.77% | 94.07% | 75.51% |
| Our package, rescore | **86.91%** | 94.07% | **79.59%** |

Our package returns exactly the same suggestion lists as the original library, so serving it
our way did not change the model. Rescore raises top-1 by about eight points, which is why it
is the background upgrade.

The published Hindi Dakshina top-1 is **60.56%**, measured on the official test list. Our
405-word sample is curated and easier, so 78.77% is not a claim to beat that figure. Running
the official list is the first item of future work.

### 7.2 Running courtroom text

The stress test typed 160 sentences (902 words). The final suggestion for each word matched
the reference spelling **82.48%** of the time, and the right word was in the top 4 **98.12%**
of the time. The figure was identical in every stress run. Most misses are spellings a reader
would accept (गये for गए, नही for नहीं, मा for मां). Real misses include `lucknow` → लकनो
(expected लखनऊ) and `woh` → वोह (expected वह).

---

## 8. Stress testing — many typists, bad links

### 8.1 Setup

* **16 sessions**, each a separate copy of the local service with its own port and package
  store, started at the same moment. Twenty would not fit in 18 GB; the rescore dictionary is
  off because 16 copies at 1.7 GB would not fit either.
* **Typing data:** `data/stress_roman_hi.tsv`, 160 romanized Hindi sentences with reference
  Devanagari, about half courtroom proceedings and half everyday speech and names. Each typist
  gets its own 10 sentences and types them letter by letter at about 4–5 keys per second, one
  lookup per key, committing each word.
* **Court link emulator:** `benchmarks/wan_proxy.py` sits between the sessions and central:
  60 ms round trip, 10 ms jitter, **100 Mbps download shared by all 16**, 20 Mbps upload, a
  connection reset about every 500 MB, and a switch to cut the link.

All 16 copies share one laptop's 11 cores. That makes this a picture of a shared court
computer, or of weak PCs. In the chosen design each typist has their own machine.

### 8.2 Scenarios and results

**Scenario A — first install, central reachable directly** (`stress_cold_direct.json`)

All 16 were serving in **14.9 s**. Each engine took about 9.2 s to load, against 2.5 s when
one loads alone: 16 loads at once on 11 cores cost about 3×.

**Scenario B — first install over the court link, then 16 typists** (`stress_fleet_cold_wan.json`)

All 16 were serving in **187.5 s**. Sixteen copies of the 138 MB package (about 2.2 GB) had to
cross one 100 Mbps link, so the download, not the model, was the wait. Five downloads were cut
mid-transfer; all five resumed from where they stopped and finished within about 2 s of the
others.

Then 16 typists, 4,045 keystrokes, zero failures: client p50 **27.5 ms**, p95 **52.4 ms**,
p99 **81.7 ms**, 0.64% of keystrokes over 100 ms, worst 198 ms.

**Scenario C — restart with the court link cut, then 16 typists** (`stress_fleet_warm_offline.json`)

All 16 booted from their saved copies in **11.2 s** with central unreachable. Typing: p95
54.8 ms, p99 83.6 ms, zero failures, the same as with the link up. The slowest keystrokes were
each typist's first one, when all 16 hit the first letter together: 171 ms median. After the
first key, p99 was 75.3 ms.

**Scenario D — same, one PyTorch thread per process** (`stress_fleet_warm_1thread.json`)

By default each copy started about 11 worker threads on 11 cores, and 16 copies fought over
the CPU. With one thread each:

| | Default threads | One thread per process |
|---|---|---|
| Client p50 | 27.8 ms | **15.2 ms** |
| Client p95 | 54.8 ms | **25.8 ms** |
| Client p99 | 83.6 ms | **35.5 ms** |
| Worst keystroke | 194 ms | 88 ms |
| Over 100 ms | 0.64% | **0%** |
| First keystroke (median) | 171 ms | 70 ms |
| Fleet ready after restart | 11.2 s | 8.8 s |

**Scenario E — one typist alone, default vs one thread** (`stress_solo_threads0/1.json`)

One session typed all 160 sentences without pauses: client p95 22.0 ms (default) vs 21.0 ms
(one thread). Model time on cache misses was 23.2 ms vs 21.7 ms p95. The default used about
320% CPU against about 6% for one thread.

### 8.3 What the stress tests show

1. **The court link only matters for installs and updates.** Typing latency was the same with
   the link up, slow, or cut.
2. **A first install at hearing time is not viable over a shared uplink.** Three minutes for 16
   PCs is the case for installing at machine setup and for the court mirror.
3. **Flaky links are survivable.** Dropped downloads resumed without errors; a restart with no
   internet worked.
4. **On a shared computer, CPU contention is the risk, and it has a cheap fix.** One thread per
   process halved p95, removed every lookup over 100 ms, and is no slower for a single typist.
   The service does not set this yet; it is the first change in future work.
5. **Load changes speed, not answers.** Word accuracy was identical in every run.

---

## 9. Cost

`results/cost_model.json`. Assumptions (estimates to verify, not quotes): 5,000 machines,
200 courts, base 138.3 MB, rescore 358.1 MB, one release per month, CDN $0.085/GB, central VM
$30/month, storage $1/month.

| Scenario | Egress per month | $/month | $/1,000 users |
|---|---|---|---|
| **Built today:** every machine downloads base + rescore | 2,424 GB | **237.03** | 47.41 |
| Weights-only release (dictionary reused) | 675 GB | 88.40 | 17.68 |
| With court mirrors (designed) | 97 GB | **39.24** | 7.85 |
| Court mirrors, weights-only | 27 GB | 33.30 | 6.66 |
| One always-on GPU instance (rejected) | — | 380.00 | 76.00 |

* The system that runs today costs about **$237/month**, under the $500 ceiling. The court
  mirror would cut that to about **$39/month**.
* Keystrokes are not in the bill: typing more costs nothing centrally.
* The GPU line already uses most of the budget for one instance with no redundancy, and it
  still fails on latency and outages.
* If 15% of courts need a fallback server, 60 boxes at $400 amortized over 36 months add
  about **$667/month** of hardware cost, separate from the central bill.

---

## 10. Reflection

### 10.1 What breaks at 10× (50,000 users)

* **Central:** polling grows to about 80 requests/s, almost all "not modified" — still small.
  With mirrors, egress is about $113/month; it grows with the number of courts, not typists.
* **Releases:** at this size a bad model release is expensive. Signed packages, staged rollout,
  and automatic rollback stop being optional.
* **Court fallback servers:** if many PCs fail the hardware check, or IT forbids local
  installs, running servers in courts (workers, TLS, on-site support, power backup) becomes
  the hard part.
* **Weak PCs:** if old machines miss the bar, the levers are beam 2, ONNX, and int8, each
  rechecked with the quality gate.

**Migration path:** add the court mirror first (cost and install time), then signed packages
and staged rollout (release safety), then court fallback servers where the hardware check
says they are needed.

### 10.2 What we traded away

* Production authentication, TLS, and the court mirror and fallback server.
* Languages beyond Hindi (packages are per language, so this is packaging work, not a redesign).
* The official Dakshina test run and a quality gate inside publishing.
* Installer and OS-service packaging; a container image or hosted demo.
* Measurements on real courtroom PCs. Every number here is from one fast laptop.

### 10.3 Known limitations of the evidence

* The 16-session tests share one laptop's CPU. They show contention and link behaviour, not
  the throughput of 16 separate PCs.
* The link emulator runs in software on the same machine. Kernel-level shaping (`dnctl` +
  `pfctl` on macOS) or a real remote host would be closer to a court network.
* The quality sample and stress sentences are curated. The reference spellings mostly drop
  nukta, so accuracy is a conservative figure, and the absolute numbers are not comparable to
  the published 60.56.
* Memory under the fleet is understated by `ps`, because macOS compresses idle pages. Size
  from the single-session figure: about 0.5 GB per base engine.

---

## 11. Future work (priority order)

| # | Work | Why |
|---|---|---|
| 1 | Set one PyTorch thread per local service by default | Measured: halves p95 on shared machines, no cost on a single PC |
| 2 | Run the official Hindi Dakshina test list; block publishing when the package differs from stock | Puts our number next to 60.56 and makes quality a release gate |
| 3 | Profile real courtroom PCs; set the hardware check thresholds | Decides how many PCs run locally and how many courts need a fallback server |
| 4 | Build the court mirror and stagger rollouts | Turns the 3-minute shared-uplink install into one download per court; $237 → $39/month |
| 5 | Signed packages, staged rollout, automatic rollback from heartbeats | Release safety at fleet scale |
| 6 | Court fallback server: several workers, TLS and login, editor failover | For weak PCs and courts that forbid local installs |
| 7 | Replace the `--no-deps` ML install with a clean lock file; add a container image | So a reviewer can stand it up without dependency workarounds |
| 8 | Beam 2, ONNX, or int8 only if weak PCs miss the bar | Each must pass the quality gate again |
| 9 | More languages; metrics export; installer for non-technical setup | Product completeness |

---

## 12. How to reproduce

```bash
# No model needed: tests, cost model, HTTP harness self-check
make setup-runtime && make demo-check

# Real model
make setup && make warm-model && make publish
make central                 # terminal 1: registry on :8700
make local                   # terminal 2: editor at http://127.0.0.1:8765/

# Benchmarks
make bench-latency           # model speed
python -m benchmarks.latency_http --url http://127.0.0.1:8765 --scenario all
python -m benchmarks.quality_dakshina --arms stock,base,rescore \
  --package-dir var/registry/hi/hi-1.0.0 --out results/quality_dakshina.json
make bench-cost

# Stress tests (central running; court link on :8701)
make wan-proxy
make bench-fleet             # 16 fresh sessions over the court link + 16 typists
```

The exact command for each stress result file is listed in
[results_summary.md](../results/results_summary.md#commands).

---

## Appendix — results files

| File | Experiment |
|---|---|
| `results/latency_baseline.json` · `latency_baseline_rescore.json` | Model speed by beam and word length |
| `results/latency_http.json` | One typist through the real service |
| `results/latency_http_selftest.json` | Harness check with a fake engine (not a speed result) |
| `results/quality_dakshina.json` | Stock vs package vs rescore quality |
| `results/cost_model.json` | Monthly cost scenarios and 10× |
| `results/stress_cold_direct.json` | Scenario A: 16 fresh installs, direct |
| `results/stress_fleet_cold_wan.json` | Scenario B: 16 fresh installs over the court link + typing |
| `results/stress_fleet_warm_offline.json` | Scenario C: restart with the link cut + typing |
| `results/stress_fleet_warm_1thread.json` | Scenario D: one thread per process + typing |
| `results/stress_solo_threads0.json` · `stress_solo_threads1.json` | Scenario E: single typist, default vs one thread |
