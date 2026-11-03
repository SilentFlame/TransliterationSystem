# ML Assignment - Runtime

## 🌟 Objective

The goal of this assignment is to **build and deploy an Indic transliteration system for Indian courtrooms** — one that serves live spelling suggestions to courtroom typists fast enough to feel instant, at a scale and cost that holds up in production.

The emphasis is on **deployment and the runtime choices behind it** — where the model runs, what it costs, and how it behaves under real courtroom conditions — *not* on training a new model.

---

## 🎬 Scenario

It's late 2024. The AI4Bharat transliteration API our courtroom editor depends on for live in-editor suggestions has gone dark — no warning, no timeline. Users type in Latin (`mera`) and pick from a ranked list of Indic-script candidates (`मेरा`, `मीरा`, `मैरा`, …) — and right now they're getting nothing.

You have the model weights. Bring transliteration back and turn it into something we can run in production — **you decide where and how it runs.** It has to hold up under real-world conditions:

- **Fast** — p95 < 100ms per lookup. Suggestions that lag behind typing are useless.
- **At scale** — ~5,000 daily active courtroom users, ~500 concurrent at peak.
- **In real courtrooms** — connectivity is often constrained or flaky.
- **Without regressing** below the model's known transliteration quality.
- **At a sensible cost** — the obvious always-on-GPU answer is expensive, and that cost grows with every courtroom we onboard. As a rough ceiling, think on the order of $500/month at this scale; we care about the cost reasoning and the order of magnitude, not hitting an exact figure.

---

## 📦 Starting Point

- **Model** — AI4Bharat's **IndicXlit** (the `XlitEngine` from the `ai4bharat-transliteration` library, also on Hugging Face). It emits a ranked list of candidate spellings. Use it as-is; retraining is out of scope.
- **Evaluation set** — a small set of romanized inputs → expected Indic outputs to sanity-check that quality doesn't regress across your deployment (start with **Hindi or something familiar**; add others if useful). The **Dakshina** romanization lexicons are a clean option, and AI4Bharat publishes IndicXlit's own results on this exact benchmark.

You're free to add other public data or apply lightweight quantization / compilation, as long as it serves the deployment and the outputs stay materially as good as the baseline.

---

## ⏳ Time Commitment

Candidates should spend no more than **2–3 days** on this assignment (~12–16 focused hours). The scope is **prototype-level**: a working, measured slice beats a broad written plan.

---

# ✍️ Task Overview

Approach it however you think is right — there's no prescribed architecture. A strong submission works through roughly this arc:

### 1. Characterize the workload

Understand what you're actually serving before you decide how to serve it.

- Describe the traffic: how requests arrive as someone types, how bursty it is, and how a single user's load compares to the aggregate across hundreds of concurrent users.
- Pin down what "p95 < 100ms per lookup at this scale" actually demands — where a request's time budget goes, and what that pressure rules in or out.

### 2. Decide where and how to run it

This is the heart of the assignment.

- Work out the viable ways to run this model, and pick one. The design space is yours to map — the hardware, tenancy, and maintenance lifecycle.
- Make the case for your choice, against the alternatives you considered and rejected: latency, cost at this scale, and behavior in real courtrooms. We care more about the quality of that reasoning than the option you land on.

### 3. Build a working prototype

Ship something that runs.

- Implement transliteration serving for your chosen approach, producing ranked suggestions end-to-end — not just calls in a notebook.
- Make it reproducible: we should be able to stand it up, or replay your benchmarks, from your instructions.

### 4. Benchmark it

Measure it against the bar you were given.

- Report latency (p50 / p95 / p99) per lookup, throughput / max concurrency, and resource use — including cold-start or time-to-first-suggestion where that matters for your approach.
- Build a cost model: cost per user (or per 1K users) per month at this scale, with your assumptions stated. Load-test against the target, not a single happy-path call.

### 5. Reflect

- What breaks at 10× the users, and what's the migration path?
- What you traded away, and what you'd revisit with more time.

# 🎯 Deliverables

1. **Report** — a clear write-up covering:
    - How you characterized the workload and what the constraints demanded of it.
    - The ways you considered running it and **why you chose the one you did** (latency, cost, behavior in courtrooms).
    - Benchmark results and your cost model.
    - Key takeaways and how you'd scale or harden it.
2. **Codebase** — clean, well-documented code for the serving prototype, the benchmark/load-test harness, and the cost model.
3. **Benchmark & Cost Results** — latency percentiles, throughput, and cost-at-scale, with assumptions stated. Include qualitative notes on suggestion quality versus the baseline.

<aside>
💡

We suggest the following media, but feel free to use your own judgement:

- **Report** → `Notion`
- **Code** → `GitHub`
- **Service / demo** → a container image, a deployed endpoint, or a hosted browser demo
</aside>

# 🏆 Scoring Criteria

1. **Approach & Feasibility**
    - Is the workload characterized correctly, and do they show they understand what the latency bar demands?
    - Is the choice of where and how to run it **justified for the stated scale and cost** — not just asserted?
2. **Code Quality**
    - Is the code well-structured, reproducible, and easy to follow?
3. **Benchmarking & Cost Modeling**
    - Are the measurements rigorous (percentiles, realistic load) rather than single-shot?
    - Is the cost model grounded and its assumptions explicit?
4. **Evaluation & Insights**
    - Does suggestion quality hold up against the baseline?
    - Are the scaling limits and next steps clear-eyed?