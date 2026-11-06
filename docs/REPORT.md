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
16-typist stress test stays inside the latency bar for a typical keystroke on a shared machine.
Pinning PyTorch to one thread per process roughly halves that tail. The lookups that still
cross 100 ms are the first keystroke, when all 16 typists start together.

| Read next | For |
|---|---|
| [DESIGN.md](DESIGN.md) | Full decision record |
| [HLD.md](HLD.md) · [LLD.md](LLD.md) | Components and module contracts |
| [../results/results_summary.md](../results/results_summary.md) | Every measured number, by file |
| [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) | Open interpretations and next steps |
| [../IterativeIdeation.md](../IterativeIdeation.md) | How the design evolved, iteration by iteration |
| [../README.md](../README.md) | Setup and commands |

All numbers below come from JSON files in `results/`, measured on an Apple M3 Pro (11 cores,
18 GB RAM). They are fast-hardware numbers; section 10 covers what changes on a weak courtroom PC.

**Dates.** Sections 6 and 7 (one engine, one typist, the 405-word quality sample) were recorded on **3 November 2026**. In the latest change the suite was re-run, and section 7 gained the official Dakshina Hindi test: 29 unit tests (all passed),
the cost model (unchanged), the HTTP harness self-test, and every stress scenario in section 8.
Section 8 quotes the latest run. The laptop was not idle that morning (another process was
already bound to port 8700), so cold-start and tail numbers are a little slower than on 3 November.
The direction of every result is the same. Later the same day the three container images were
built, run on this machine, and pushed to GHCR (section 4, section 13).

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

**Container images (latest change).** One Dockerfile builds three images, published for
`linux/amd64` and `linux/arm64` as `ghcr.io/silentflame/transliterationsystem:{offline,central,local}`.
`offline` has the Hindi base package inside and boots with central unreachable. `central` is the
registry with `hi-1.0.0` baked in. `local` starts empty, downloads base, then serves. Rescore is
off, so a container stays near 0.5 GB. On this Mac the offline image was healthy in 3–5 s
(~450 MB) and returned the same Hindi candidates as the native service; the compose stack
downloaded 138 MB from central in ~230 ms and served its first suggestion in ~2.5 s; stopping
central and restarting local served from the saved store in 3 s. The amd64 image, run under
emulation, returned the same candidates. Commands are in the README. The GHCR package is still
private: a clean machine needs a one-time visibility change or `docker login ghcr.io`.

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

The published Hindi Dakshina top-1 is **60.56%**, on the official test list. The 405-word
sample is curated and easier, so 78.77% is not that figure.

In the latest change the same harness scored the official list
(`hi.translit.sampled.test.tsv`: 4,502 attested pairs, 2,500 words). Each pair is one example,
exact string match, beam 4. Result: `results/quality_dakshina_hi_test.json`.

| Engine | Official test top-1 | top-4 |
|---|---|---|
| Original library | **60.33%** | 84.65% |
| Our package, base | **60.33%** | 84.65% |
| Our package, rescore | **72.26%** | 84.65% |

Base returns the same candidate lists as the library. 60.33% sits next to the published
60.56% (about 10 pairs out of 4,502). Rescore, which the typist service loads in the
background, is 12 points higher on the same list.

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

Latest re-run, 11:23–11:32 IST. Registry on port 8710, court-link emulator on 8711
(port 8700 was already in use). Zero failed lookups in every typing run. Word top-1 stayed
**82.48%** and top-4 **98.12%** in every run.

### 8.2 Scenarios and results

**Scenario A — first install, central reachable directly** (`stress_cold_direct.json`)

All 16 were serving in **21.0 s**. Each engine took about 14.1 s to load, against 3.0 s when
one loads alone.

**Scenario B — first install over the court link, then 16 typists** (`stress_fleet_cold_wan.json`)

All 16 were serving in **194 s**. Sixteen copies of the 138 MB package (about 2.2 GB) had to
cross one 100 Mbps link, so the download, not the model, was the wait. Three downloads were cut
mid-transfer; all three resumed and the fleet still finished together.

Then 16 typists, 4,045 keystrokes: client p50 **29.4 ms**, p95 **71.9 ms**, p99 **194 ms**,
2.8% of keystrokes over 100 ms, worst 323 ms. This run started immediately after 16 engines
had loaded at once, so the tail is the contended machine, not the link.

**Scenario C — restart with the court link cut, then 16 typists** (`stress_fleet_warm_offline.json`)

All 16 booted from their saved copies in **11.6 s** with central unreachable (16 offline
events, source `local_cache`). Typing: p50 29.4 ms, p95 **64.7 ms**, p99 110 ms, 1.5% over
100 ms, worst 203 ms. The slow keystrokes are each typist's first one, when all 16 hit the
first letter together: 149 ms median. After that first key, p99 is 102 ms.

**Scenario D — same, one PyTorch thread per process** (`stress_fleet_warm_1thread.json`)

By default each copy starts about 11 worker threads on 11 cores, and 16 copies fight over the
CPU. With one thread each, against the link-down run above:

| | Default threads (link down) | One thread per process |
|---|---|---|
| Client p50 | 29.4 ms | **16.1 ms** |
| Client p95 | 64.7 ms | **29.2 ms** |
| Client p99 | 110 ms | **53.3 ms** |
| Worst keystroke | 203 ms | 273 ms |
| Over 100 ms | 1.5% | **0.4%** |
| First keystroke (median) | 149 ms | 204 ms |
| After the first key, p99 | 102 ms | **43.6 ms** |
| Fleet ready after restart | 11.6 s | 14.0 s |

**Scenario E — one typist alone, default vs one thread** (`stress_solo_threads0/1.json`)

One session typed all 160 sentences without pauses: client p95 **26.7 ms** (default) vs
**21.5 ms** (one thread). Model time on cache misses was 30.6 ms vs 23.3 ms p95. The default
used about 284% CPU against about 6% for one thread. With one thread the slowest keystroke
was 52 ms.

### 8.3 What the stress tests show

1. **A dead link does not stop suggestions.** With central unreachable, all 16 sessions booted
   from disk and served 4,045 keystrokes with zero failures.
2. **A first install at hearing time is not viable over a shared uplink.** About 3.2 minutes
   for 16 PCs is the case for installing at machine setup and for the court mirror.
3. **Flaky links are survivable.** Three downloads were reset mid-transfer, all three resumed,
   and no lookup failed.
4. **On a shared computer, CPU contention is the risk, and one thread per process is the cheap
   fix.** It cut p95 from 65 ms to 29 ms. It did not remove every lookup over 100 ms on this
   re-run: 0.4% still crossed it, and those are the simultaneous first keystrokes. After the
   first key, p99 is 44 ms. For one typist alone, one thread is as fast and uses far less CPU.
   The service does not set this yet.
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
* A publish-time gate that refuses to move `LATEST` when the package differs from stock on the Dakshina test list. The list itself was run in the latest change (section 7).
* Installer and OS-service packaging. The container images are published; there is no public URL.
* Measurements on real courtroom PCs. Every number here is from one fast laptop.

### 10.3 Known limitations of the evidence

* The 16-session tests share one laptop's CPU. They show contention and link behaviour, not
  the throughput of 16 separate PCs.
* The link emulator runs in software on the same machine. Kernel-level shaping (`dnctl` +
  `pfctl` on macOS) or a real remote host would be closer to a court network.
* The 405-word sample and the stress sentences are curated. The official test split is the
  number next to 60.56 (section 7). The stress-sentence accuracy is a separate check.
* Memory under the fleet is understated by `ps`, because macOS compresses idle pages. Size
  from the single-session figure: about 0.5 GB per base engine.

---

## 11. Future work (priority order)

| # | Work | Why |
|---|---|---|
| 1 | Set one PyTorch thread per local service by default | Latest re-run: shared-machine p95 65 → 29 ms. The lookups still over 100 ms are the simultaneous first keystroke. No cost for one typist. |
| 2 | Block publishing when the package differs from stock on the Dakshina test list | The list was run in the latest change (60.33% vs published 60.56%, lists identical). The check is not yet a gate on `LATEST` |
| 3 | Profile real courtroom PCs; set the hardware check thresholds | Decides how many PCs run locally and how many courts need a fallback server |
| 4 | Build the court mirror and stagger rollouts | Turns the 3-minute shared-uplink install into one download per court; $237 → $39/month |
| 5 | Signed packages, staged rollout, automatic rollback from heartbeats | Release safety at fleet scale |
| 6 | Court fallback server: several workers, TLS and login, editor failover | For weak PCs and courts that forbid local installs |
| 7 | Make the GHCR package public, and give `make setup` the CPU torch wheel on x86 | The images are published but still private. A CPU-only x86 VM that installs from `requirements-ml.txt` directly still gets the CUDA torch wheel; the Dockerfile already avoids that |
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

# No Python install: published images (package is private until its visibility is changed)
docker pull ghcr.io/silentflame/transliterationsystem:offline
docker run --rm -p 8765:8765 ghcr.io/silentflame/transliterationsystem:offline
```

The exact command for each stress result file is listed in
[results_summary.md](../results/results_summary.md#commands).

---

## 13. Assignment coverage (checked again after the images, latest change)

Twelve asks, scored from the table below. **11 are done and 1 is partial. Counting a partial as half, that is 11.5 / 12 ≈ 96%.** The suggested demo (a container, a deployed endpoint, or a hosted browser demo) is met by the images and the walkthrough video. The official Dakshina Hindi test was run in the latest change (section 7): packaged base is 60.33% top-1 against the published 60.56%, with the same candidate lists as the library. The remaining partial is load measured at 16 sessions rather than 500 machines. It does not change the runtime choice.

| Assignment ask | Status |
|---|---|
| Characterize the workload and the 100 ms budget | Done. Section 1. |
| Choose where it runs, against the alternatives | Done. Sections 2–3 and [DESIGN.md](DESIGN.md). |
| Working prototype, ranked suggestions end to end | Done. Editor, local service, central registry. 29 tests passed in the latest change. |
| Reproducible setup and benchmark replay | Done. `make setup` still needs Python 3.10, `pip<24.1`, and `--no-deps`. The images are the path that does not need that: built and run here, pushed for amd64 and arm64. A `docker pull` of `:offline` on this Mac succeeded; the following `docker run` failed because Colima's socket was down, which is the host engine, not the image. |
| Latency percentiles per lookup | Done. One typist (3 Nov) and 16 typists (latest change). |
| Cold start and time to first suggestion | Done. Section 8, re-measured in the latest change. Inside the image: offline ready in 3–5 s; a fresh local session's first suggestion ~2.5 s after central answered. |
| Resource use | Done for the stress runs (RSS and CPU in the JSON). The offline container sat near 450 MB. |
| Throughput / concurrency, load-tested rather than one call | Partial. 16 real services on one laptop over a simulated court link, about 59 lookups/s. Not 500 independent PCs, and not a saturation curve. The design treats 500 concurrent as 500 machines. |
| Cost model with stated assumptions | Done. Re-checked in the latest change: $237/month for what ships, $39/month with the unbuilt court mirror. |
| Quality against the known baseline | Done. Official Dakshina Hindi test, latest change: stock and packaged base both 60.33% top-1 / 84.65% top-4, lists identical. Published IndicXlit Hindi top-1 is 60.56%. Rescore on the same list is 72.26% top-1. The 405-word sample (3 Nov) remains the quick packaging check. |
| What breaks at 10×, and what was traded away | Done. Section 10. |
| Container, deployed endpoint, or hosted browser demo | Done as a container plus a video. Images: `ghcr.io/silentflame/transliterationsystem:{offline,central,local}`, both architectures. Walkthrough: [../docs/demo/courtroom-editor-demo.mp4](../docs/demo/courtroom-editor-demo.mp4). The package is still private, and there is no public URL. |

Left for later, and called out as such: one torch thread as the service default, a publish gate on the Dakshina test list, a profile of real courtroom PCs, the court mirror, signed packages and staged rollout, the court fallback server (workers, TLS, editor failover), a public GHCR package and a CPU-only `make setup` on x86, and languages beyond Hindi.

---

## Appendix — results files

| File | Experiment |
|---|---|
| `results/latency_baseline.json` · `latency_baseline_rescore.json` | Model speed by beam and word length |
| `results/latency_http.json` | One typist through the real service |
| `results/latency_http_selftest.json` | Harness check with a fake engine (not a speed result) |
| `results/quality_dakshina.json` | Stock vs package vs rescore on the 405-word sample |
| `results/quality_dakshina_hi_test.json` | Same arms on the official Dakshina Hindi test split (latest change) |
| `results/cost_model.json` | Monthly cost scenarios and 10× |
| `results/stress_cold_direct.json` | Scenario A: 16 fresh installs, direct |
| `results/stress_fleet_cold_wan.json` | Scenario B: 16 fresh installs over the court link + typing |
| `results/stress_fleet_warm_offline.json` | Scenario C: restart with the link cut + typing |
| `results/stress_fleet_warm_1thread.json` | Scenario D: one thread per process + typing |
| `results/stress_solo_threads0.json` · `stress_solo_threads1.json` | Scenario E: single typist, default vs one thread |
