# Faultline PRD v6

2026-09-19 · @Someone

## Summary

**When everything breaks at once, dashboards can't tell you why. Faultline runs the experiment that can, and that experiment is usually the fix.**

Faultline is an autonomous experimental debugger for distributed systems. It plugs into any OpenTelemetry-instrumented system and works in four beats:

1. **Surface and prove the problem.** Triage considers every class of sustaining cause (taxonomy below), rules out what telemetry can rule out and names the metric that did it, and says plainly when the survivors are indistinguishable.
2. **Fork production; crawl the clones.** Faultline forks the system into disposable clones built only from observable config, never production data. One investigator agent per hypothesis crawls its clone hunting for the outage: it injects its suspected cause and must recreate the production fingerprint. A theory that can't reproduce the incident dies before production is touched. Survivors are measured against every candidate intervention.
3. **Aimed chaos, then one production test.** Every fault Faultline injects is a bet two theories disagree on. The clones pick the gentlest production probe that separates them; it runs for seconds, with a TTL, and measurement against noise decides. If nothing passes its own confirmation test, Faultline says *none of the above* and pages a human.
4. **Patch, attack the patch, ship.** Reversible mitigation stays in place; Devin writes the durable fix; in a fresh clone Faultline replays the reproduced incident and an investigator runs *educated chaos* against the patch, trying to break it. Only a fix that survives goes to a self-verified 5 % canary. Irreversible remediations (split a hot shard, resize a tier) are never done autonomously: Faultline writes the case and a human signs.

What changed in v6.1 (2026-09-19 15:45; refinements, no new subsystem):

- **Incident replay suite.** Every resolved incident leaves a durable artifact: the C6 recipe that reproduced it in a clone. Future patches must survive the whole suite before they may canary. Faultline's incident history becomes a regression suite for the class of failure it just handled.
- **Attack the patch.** Patch verification reuses the investigator loop: one investigator is given the hypothesis "this patch prevents the incident" and tries to falsify it (bigger trigger, higher load, tighter timeout), with predicted outcomes and measured verdicts. A measured counterexample goes back to Devin's session.
- **Planner tradeoff is visible.** The UI shows the candidate table at stage 4b: per lever, expected separation in σ (measured in clones), blast radius, and why the winner won.
- **No standalone chaos mode.** Experiments without a hypothesis have no prediction and therefore nothing to judge; that is chaos tooling's category, not ours.
- **Identity restated as four beats** (surface and prove → fork and crawl → aimed chaos and one production test → patch, attack the patch, ship); **sustaining-cause taxonomy** so triage is exhaustive over the hypothesis space (`considered` field proposed for C2); **three-tier autonomy ladder** with human-gated irreversible remediations (shard split as the example); **depth-on-the-storm stretch list** (find the cliff, outage-as-test, scaling counterfactual, admission control at measured capacity).

What changed from v5 (v4 → v5 changes kept below):

- **Clone lab.** Observation tells you *what* is happening, not always *why*, and production is too risky for aggressive experiments. Faultline now spins up disposable, healthy copies of the system where investigator agents try to reproduce each hypothesis, run counterfactual experiments and attack Devin's patch before anything touches production.
- **Evidence chain:** passive telemetry ambiguous → clones reproduce each hypothesis and measure how each responds to candidate experiments → a short, controlled production probe confirms → mitigation. The production experiment stays; the clones make it better chosen and its predictions measured rather than guessed.
- **Three environments,** strictly separated: production (hidden real cause, C3 levers only), clean clones (C6 lab actions), benchmark controller (C5, hidden from everyone).
- **Fixes are verified twice:** replayed against the reproduced incident in a clone, then canaried in production.
- **Identity:** an autonomous experimental debugger: it gives debugging agents disposable copies of a failure so they can run counterfactual experiments, falsify hypotheses and validate fixes.

What changed from v4:

- **Plug-in, not a bespoke sandbox.** Faultline talks to a system only through adapters. The sandbox is treated as a customer system.
- **New hero scenario.** Self-sustaining retry storm vs. degraded DB, which stays ambiguous under full, realistic telemetry. The v4 fraud-check vs. DB pair leaked through traces and becomes the easy case.
- **Predictions come from the LLM and the service graph,** judged against measured noise. The calibrated response library and rearm oracle are cut. (Clones were cut in v5; v6 brings back a minimal clone lab.)
- **The LLM proposes and explains; measurement decides.**
- **Two-tier remediation with autonomy:** reversible mitigations run immediately; code ships only through a canary Faultline verifies itself.
- **Question changed** from "what started it" to "what is sustaining it now."
- **Benchmark:** own sandbox set plus the public OpenTelemetry Demo.

## Problem

In distributed systems one failure spreads: a slow component causes timeouts, timeouts cause retries, retries cause overload. By the time an engineer looks, every service is red and several causes fit the same symptoms.

The worst version is a **metastable failure**: the trigger is gone, but the system stays broken because retries keep it overloaded, like a traffic jam that outlasts the accident.

- At least 4 of 15 major AWS outages in a decade were metastable, lasting 1.5 to 73 hours ([OSDI '22](https://www.usenix.org/conference/osdi22/presentation/huang-lexiang), [summary](https://muratbuffalo.blogspot.com/2026/07/characterizing-metastable-faults-and.html)).
- Retry policy was a sustaining cause in about half the studied incidents ([summary](https://www.micahlerner.com/2022/07/11/metastable-failures-in-the-wild.html)).
- Researchers describe detecting and recovering from them as open problems ([Penn State](https://sites.psu.edu/timothyz/metastable-failures/)).
- On live SRE benchmarks, agents often mitigate without a correct diagnosis ([SREGym](https://benchmarklist.com/benchmarks/sregym/)), and frontier models score below 50% on incident root-cause analysis ([ITBench-AA](https://artificialanalysis.ai/articles/itbench-aa-launch)).

**Why dashboards can't answer it.** The key question is often counterfactual: *is the DB slow because we're overloading it, or are we overloading it because it's slow?* A saturated DB looks "full" either way. The only way to learn what latency would be at normal load is to lower the load and look.

## Product

Faultline runs an 8-stage pipeline; experiments run only when triage says the incident is ambiguous. Stage numbering is unchanged from v5 (the C4 audit contract depends on it); the clone probe is part of stage 4.

```mermaid
flowchart LR
  A[1 Ingest] --> B[2 Detect]
  B --> C[3 Triage]
  C -->|obvious| E[5 Mitigate]
  C -->|ambiguous| D1[4a Probe in clones]
  D1 --> D2[4b Confirm in production]
  D2 --> E
  E --> F[6 Patch via Devin]
  F --> G[7 Canary + verify]
  G --> H[8 Report]
```

| Stage | What happens |
| --- | --- |
| 1 Ingest | Traces, metrics, logs and change events via OpenTelemetry, stored in Elasticsearch |
| 2 Detect | SLO alert fires, e.g. checkout p99 above 1 s for 60 s |
| 3 Triage | Service graph + LLM rank candidate causes; obvious ones skip to stage 5 |
| 4 Experiment | **4a Probe:** per hypothesis, an investigator agent gets a clean clone, injects the hypothesized cause, tries to reproduce the production fingerprint, and measures how that world responds to candidate experiments. **4b Confirm:** apply the gentlest production intervention that separates the surviving hypotheses (predictions now measured in clones) and judge the response against noise |
| 5 Mitigate | Keep a reversible action, often the experiment that won |
| 6 Patch | Devin writes the durable fix as a PR; in a fresh clone the patch must survive the **incident replay suite** (this incident's reproduction recipe plus every earlier one) and an investigator's attempt to break it, before it may canary |
| 7 Canary | New version gets \~5% of traffic, compared against old; auto-promote or auto-revert |
| 8 Report | Timeline, evidence, actions, PR link, for human review |

Core principles:

- **Plug-in.** Three adapters only: telemetry in, actions out, code via GitHub + Devin. Faultline never imports the target's code.
- **Find what sustains it.** Vocabulary is **trigger** (what started it) and **sustaining cause** (what keeps it broken now).
- **Confirmation, not elimination.** A diagnosis must pass its own positive test.
- **Admit ignorance.** If no hypothesis passes its confirmation test, report none-of-the-above and page a human with the evidence.
- **Prefer diagnoses you can reproduce.** A hypothesis earns support by reproducing the production fingerprint in a clean clone from its own injected cause, not by LLM confidence.
- **Aggressive in clones, gentle in production.** Clones may disable retries, halve capacity, kill services and replay the incident repeatedly; production only gets C3 levers with TTLs.
- **Nothing goes unconsidered.** Triage reports a verdict on every class in the sustaining-cause taxonomy, not just the ones it likes.

### Sustaining-cause taxonomy (what triage must consider)

Exhaustiveness lives in the *hypothesis space*, not in how many worlds we build. Triage returns a verdict for every class: `live`, `ruled_out` (naming the metric that ruled it out), or `cant_tell` (the signal isn't collected). The LLM may add an `other` class with a justification; nothing may be silently skipped. `cant_tell` classes are surfaced to the human in the report. This list is a starting set and will grow; the rule is that every entry names its signature, its discriminating experiment, and its autonomy tier.

| Class | Telemetry signature | Discriminating experiment | Reproducible in a clone? | Tier |
| --- | --- | --- | --- | --- |
| Retry storm (metastable) | high retry ratio, DB saturated, trigger gone | cap retries, release | yes (`db_latency`) | auto |
| Degraded dependency / DB | DB saturated at low issued qps | failover / pause batch | yes (`db_capacity`) | auto |
| Resource exhaustion (CPU, pool, FDs) | service p99 up, DB fine | `cpu_limit` in clone; scale in production | yes | auto / describe |
| Bad deploy / config | change event precedes SLO breach | roll back canary | yes (`patch_ref`) | auto |
| Queue backlog | consumer lag grows, producer fine | pause producer / drain | partial | describe |
| Hot key / hot shard | one key or partition dominates DB time | rate-limit key; split shard | partial | **human-gated** |
| Cache stampede | miss spike, DB qps burst at TTL edge | coalesce / jitter TTL | partial | describe |
| Bad node / noisy neighbor | one instance anomalous, peers fine | drain instance | yes (`service_kill`) | auto |
| Other (LLM-proposed) | stated by the LLM | stated by the LLM, or `cant_tell` | — | human |

In the hero: 8 classes considered, 2 live and indistinguishable (storm, degraded DB), 5 ruled out with a metric each, CPU starvation `cant_tell` because per-container CPU is not collected. That is what makes the later *none of the above* on the CPU world credible: it isn't "neither of my two guesses", it is "nothing in the taxonomy passed".

*Contract impact (C2, needs Owner 3 sign-off):* add `considered: list[ClassVerdict {class, verdict: live|ruled_out|cant_tell, evidence_metric?, note}]` to `TriageDraft`; UI shows it as a row under the hypotheses panel.

## Hero scenario: storm vs. degraded DB

The hero is two hidden worlds that look identical on the dashboard and respond oppositely to one cheap experiment: cap retries, then release.

**Setup (illustrative numbers).** Users send 80 checkouts/s. Orders → Payments → one Postgres query each. DB comfortably handles \~100 queries/s. Orders times out at 500 ms and retries up to 3 times (4 attempts).

**World A: self-sustaining storm (H\_meta).** A 20 s DB hiccup pushes queries past 500 ms. Orders retries; timed-out queries still run in the DB, so load climbs to \~320/s. The hiccup ends and the DB is healthy again, but 320/s keeps it overloaded, so timeouts and retries continue indefinitely.

**World B: degraded DB (H\_db).** A runaway batch job cuts the DB to \~40 queries/s for the app. 80/s exceeds that, so timeouts and retries push load to \~320/s.

| Dashboard signal | World A | World B |
| --- | --- | --- |
| Queries/s at DB | \~320 | \~320 |
| Retries per request | \~4 | \~4 |
| DB query time | > 500 ms | > 500 ms |
| Pool / DB busy | 100% | 100% |
| Checkout errors | very high | very high |
| Hidden truth: DB capacity | \~100/s | \~40/s |

**The experiment: cap retries at 0 for \~20 s, then remove the cap.**

| Measurement | World A | World B |
| --- | --- | --- |
| DB query time while capped | Falls to normal | Stays high |
| After the cap is removed | Stays healthy (loop broken) | Storm returns |
| Diagnosis | Storm; the cap was the fix | DB degraded; fix the DB |
| Confirmation test | Stays healthy after release | DB failover / pause job restores it |

The experiment is cheap: during a storm nearly every checkout already fails. In World A it is also the mitigation.

**What the clones add in the hero.** Both hypotheses reproduce the steady-state fingerprint in a clone (that is what makes the hero ambiguous, and why the ambiguity check below still applies). Clones therefore don't pick the winner on their own. They (1) show both hypotheses are live, (2) *measure* each world's response to each candidate production experiment, replacing LLM-guessed direction predictions, (3) select the gentlest production probe that separates them, and (4) falsify hypotheses that fail to reproduce (e.g. CPU starvation doesn't saturate the DB pool). The production retry cap is still the discriminating, confirming test.

**Go/no-go gate (first \~3 hours). Status: passed** (storm persists 60 s+ after the trigger; retry cap heals it permanently, 5/5 runs; see `sandbox/README.md`). Starting settings: DB `cpus: 0.5`, client timeout 300–500 ms, 3 retries, no backoff, load near capacity; trigger with a 20 s DB delay. Pass = storm persists 60 s+ after the trigger ends, and capping retries ends it permanently. Start from the metastability researchers' examples ([repo](https://github.com/lexiangh/Metastability)); credit any reused code.

**Fallback hero.** If the gate fails: slow fraud-check dependency vs. slow DB, with the fraud check as an uninstrumented vendor library (no span), a stated assumption.

**Ambiguity check.** Before building further, run a nearest-centroid classifier and a passive LLM on both worlds' steady-state telemetry. If either separates them reliably, move World B's cause somewhere typically uninstrumented.

## Clone lab (stage 4a)

The clone lab is where Faultline generates evidence that production telemetry can't give it, without risking users.

**Three environments, never mixed:**

| Environment | Knows the hidden cause? | Who acts on it | Allowed actions |
| --- | --- | --- | --- |
| Production | Yes, but invisible to Faultline and investigators | Faultline orchestrator | C3 levers only (retry cap, shed, DB failover, canary), each with a TTL |
| Clean clone | No, starts healthy | One investigator agent | C6 lab actions, clone-only |
| Benchmark controller | Yes (it injected it) | bench/ and the demo script | C5 faults on production; never visible to Faultline |

**What a clone inherits.** Only what Faultline may legitimately know: service versions and images, config, retry/timeout policy, topology, and a replayable workload (request rate and pattern reconstructed from telemetry). **Never** the hidden fault or fault-controller state; copying the faulty environment would leak ground truth and make reproduction meaningless.

**Investigator loop** (one agent per hypothesis, OpenAI API): given the hypothesis, its clone, the service graph, the production fingerprint, prior experiment history, the C6 action catalog and an experiment budget:

observe → choose experiment → predict outcome → execute in clone → measure → compare → update belief

Experiments are chosen because hypotheses predict different outcomes for them, not as random chaos testing.

**Evidence rules** (what counts; judged by math, not the LLM):

1. **Reproduces:** the hypothesized cause, injected into a clean clone, produces a fingerprint within noise of production.
2. **Predicts:** the hypothesis correctly predicts the clone's response to an intervention.
3. **Survives falsification:** it survives an experiment designed to break it.
4. **Explains recovery:** removing the hypothesized condition restores the clone.
5. **Confirmed in production:** the production probe (4b) behaves as the clone measurements predicted.

A hypothesis that fails 1 is dropped before touching production. If none reaches 5: none-of-the-above, page a human.

**C6 action catalog (clone-only, \~6–9 primitives):** create/reset/destroy clone, replay workload, set retry count, set timeout, inject dependency/DB latency, change DB/CPU capacity, pause/resume batch workload, restart/kill service, clone metadata/status. **C6 actions are never production actions**, and C6 never exposes the hidden world.

**Reproduction recipes and the replay suite.** When a hypothesis reproduces the production fingerprint (evidence rule 1), the C6 actions and workload that did it are saved as a **reproduction recipe** (e.g. `db_latency 800 ms / 20 s @ 80 rps`, judged within noise of the incident fingerprint). Recipes accumulate into the **incident replay suite**. It is built from nothing new: clone reset, workload replay and the judge.

**Patch verification = replay + attack.** Devin's patch runs as orders-v2 in a fresh clone.

1. *Replay:* the whole replay suite runs against it; each recipe must now fail to reproduce the incident (judged against the healthy baseline, not eyeballed).
2. *Attack:* one investigator gets the hypothesis "this patch prevents the incident", the C6 catalog and a small budget, and tries to falsify it: larger or longer trigger, higher load, tighter timeout, CPU degradation. Each attempt carries a prediction and a measured verdict, the same loop as stage 4a.

Only a patch that survives both goes to the production canary. A failure returns the measured counterexample (recipe, params, observations) to the same Devin session for revision. In the pitch: *Faultline tries to break its own fix before it ships.*

**Budget.** Production + 2 concurrent investigation clones; 3 clones maximum after profiling on the final demo machine (each clone is \~8 containers). No swarm: two visible investigators communicate the idea.

**Not building:** VM snapshots, Kubernetes, full production traffic capture, arbitrary shell agents, dozens of chaos primitives, large multi-agent swarms, complex Bayesian inference, perfect environment reconstruction, a standalone chaos mode on healthy production (no hypothesis → no prediction → nothing to judge).

## Other scenarios

Two supporting scenarios show that Faultline handles easy incidents cheaply and admits when an incident fits nothing it knows.

| Scenario | Setup | Expected behavior | Priority |
| --- | --- | --- | --- |
| Easy case | Fraud-check dependency slow, visible as its own span | Triage names it with no experiment; mitigation = fail over the dependency | If ahead |
| None-of-the-above | Payments CPU starved (noisy neighbor); per-container CPU not collected | Looks like the hero; fails both confirmation tests (cascade returns after retry release; DB failover does nothing) → none-of-the-above, page a human. In clones, neither H_meta nor H_db reproduction matches it perfectly | Benchmark, maybe demo |

None-of-the-above rule: **no hypothesis passed its own confirmation test.** On the OpenTelemetry Demo, any flag outside the hypothesis list (e.g. Kafka consumer lag) serves the same purpose.

## Levers and how fixes are applied

Every *production* experiment, mitigation and code fix is a **lever** pulled through an adapter (C3), with a registered undo, a blast radius and a speed. Clone experiments use the separate C6 lab catalog and never touch production.

| Lever | Examples | Sandbox mechanism | Build? |
| --- | --- | --- | --- |
| Traffic | Shed 10% / 50%, canary split | Envoy weights and rate limits | Yes |
| Call policy | Cap retries, timeouts, backoff | Orders runtime override endpoint | Yes |
| Dependencies | Fail over fraud check, DB failover / pause batch job | Config switch; pause the load job | Yes |
| Code | Backoff + jitter, timeout + circuit breaker | Devin PR → orders-v2 container → Envoy canary | Yes |
| Capacity | Scale replicas, grow pool | `docker compose --scale`, pool config | Describe only |
| Changes | Roll back deploy, revert flag | Image tags, flags file | Describe only |
| State | Restart, kill stuck query | `docker restart`, Postgres admin | Describe only |

**Lifecycle, identical for every lever:**

1. Propose (LLM, from the adapter's catalog).
2. Safety check: reversible, within action budget, acceptable blast radius. Otherwise refused.
3. Apply via adapter.
4. Watch 15–30 s (config) or a few minutes (canary).
5. Judge: did target metrics move as predicted, beyond noise?
6. Keep if better; run the undo if worse or unclear.
7. Record in the audit log.

**Code = same loop plus clone verification plus a canary.** Devin opens a PR; Faultline first runs it as orders-v2 in a fresh clone against the replayed incident and stress variants (fails → evidence back to Devin); then builds orders-v2 beside orders-v1 in production; Envoy sends 5% to v2; compare v2 vs v1; promote gradually or set v2 weight to 0. If verification fails, send the measured evidence back into the same Devin session for a revision. A prebuilt fallback patch keeps the demo independent of Devin's latency.

**Autonomy ladder and guardrails.**

| Tier | What | Who decides | Examples |
| --- | --- | --- | --- |
| Automatic | telemetry reads, triage, clone experiments, reversible production actions with a TTL | Faultline | retry cap, shed, DB failover |
| Canary-gated | code | Faultline, after clone replay + patch attack, then 5 % canary it judges itself | Devin's backoff + circuit-breaker patch |
| Human-gated | irreversible or capacity-changing remediations | a human signs; Faultline writes the case | split a hot shard, resize a tier, schema change |
| Human after the fact | merging, refactoring, reviewing the report | a human | — |

For a human-gated action Faultline produces the *case*, not the action: the evidence that points at it, a clone run showing it relieves the reproduced incident where that is reproducible, the runbook, and a one-click approval that is recorded in the audit log with who signed. Pitch line: *it runs the safe fixes itself, canaries the code, and writes the case for the risky ones.*

- Never irreversible actions autonomously; action budget of 5 per incident, then page a human; auto-undo on regression; kill switch; full audit log.

Coverage: the performance and availability class (bad changes, overload, slow dependencies, metastable storms, resource exhaustion, queue backlog, bad nodes, hot keys, cache stampedes). Out of scope: silent wrong answers, data corruption and consistency bugs, slow-burn leaks, systems with no levers.

## LLM vs. measurement

The LLM proposes and explains; measurement decides. This keeps diagnoses checkable and answers the judge's "isn't this just an LLM guessing?"

| Job | Owner |
| --- | --- |
| Summarize telemetry fingerprint and logs | LLM (OpenAI API) |
| Propose hypotheses | LLM |
| Predict each hypothesis's response to each candidate lever | LLM, structured output |
| Propose candidate experiments from the adapter catalog | LLM |
| Estimate baseline noise | Math |
| Rank experiments: separation vs. blast radius | Math |
| Judge result against noise; update support; confirmation check | Math |
| Investigator: choose the next clone experiment and predict its outcome | LLM (one agent per hypothesis) |
| Reproduction similarity, falsification verdicts in clones | Math |
| Patch attack: choose the next attempt to break the patch, predict its outcome | LLM (same investigator loop) |
| Replay-suite pass/fail, patch-attack verdicts | Math |
| Predictions for the production probe | Measured in clones (LLM fallback if no clone ran) |
| Mitigation choice, Devin request, report | LLM + Devin |

**Prediction schema (per hypothesis × experiment):**

```json
{"hypothesis": "H_meta", "experiment": "cap_retries_0_20s",
 "during": {"db_query_p50": "down", "db_qps": "down", "checkout_errors": "down"},
 "after_release": {"db_query_p50": "flat", "retry_ratio": "flat"},
 "confirms_if": "after_release stays at baseline"}
```

**Noise.** Record several healthy windows; per metric, noise σ = max(measured std, 10% of typical value). A change counts only if it exceeds \~3σ in the predicted direction.

**Planner.** For each candidate lever: separation = number of metrics where hypotheses predict different directions, weighted by how far each is expected to move in σ units. Score = separation − λ · blast radius (% of user requests affected). Pick the smallest intervention that clears the noise, e.g. retry cap (0% of successful requests dropped) before shedding 50%.

**Support.** Uniform prior; each experiment updates support from agreement between predicted and measured directions. Declare a diagnosis only when the leader passes its confirmation test; if none passes, none-of-the-above.

The UI shows two panels side by side: *LLM reasoning* and *measured evidence*. At stage 4b it also shows the **planner's candidate table**: one row per lever with expected separation (σ, measured in clones or LLM-predicted with that marked), blast radius (% of user requests), score, and the winner highlighted. This is the moment that separates Faultline from chaos tooling: not "it can inject faults" but "it picks the cheapest intervention per bit of information."

## Architecture

Two separate codebases: the **target system** (sandbox) and **Faultline**, which touches the target only through adapters.

```mermaid
flowchart LR
  LG[Load generator] --> EV[Envoy]
  EV --> O[Orders v1 / v2]
  O --> EV2[Envoy]
  EV2 --> P[Payments]
  P --> DB[(Postgres)]
  P --> FC[Fraud check]
  O & P --> OC[OTel Collector]
  OC --> ES[(Elasticsearch)]
  ES --> FL[Faultline]
  FL -->|actions| EV
  FL -->|PR| DV[Devin]
```

**Target system (Docker Compose):**

- Services: Gateway/Envoy, Orders, Payments, fraud-check stub, Postgres (limited CPU), load generator. FastAPI, trivial business logic.
- Envoy in front of Orders and between Orders and Payments: timeouts, traffic weights, shedding, canary split. Retries live in Orders' own code (the thing Devin patches), with a runtime override endpoint for the retry-cap lever.
- Fault controller (hidden from Faultline): DB delay trigger, batch-job load (World B), CPU limit (none-of-the-above).
- **Clone runtime:** isolated Compose replicas of the target (own project, network and ports), each with its own lab API (C6): create/reset/destroy, workload replay, lab primitives, patched-version slot.
- **Two telemetry roles, one contract.** `/stats` → `fingerprint_from_stats` is the canonical C1 source for the loop and the benchmark through the freeze (deterministic, no ingest lag). OTel auto-instrumentation → Collector → Elastic Cloud is the evidence layer: raw traces/metrics/logs a judge can inspect in Kibana behind every Faultline decision, and what makes Faultline pluggable. *If ahead:* `fingerprint_from_otel` behind the same `TelemetrySource` protocol, validated against `fingerprint_from_stats` on the same windows before it feeds anything.

### Elastic tools we will use (Owner 2)

Docker Compose runs the target system and an OpenTelemetry Collector. **Elastic Cloud is the managed remote Elasticsearch deployment** the Collector and Faultline connect to; it is not a Docker container. A local Elasticsearch container remains an optional offline-development fallback, but the demo uses an Elastic Cloud URL and API key so the team can demonstrate real Elastic queries.

- **Elastic Cloud (core):** our managed Elasticsearch and Kibana deployment. It holds the raw OpenTelemetry data plus Faultline's `faultline-fingerprints` (C1) and `faultline-audit` (C4) indices, giving us durable searchable evidence without operating a production search cluster ourselves.
- **OpenTelemetry Collector / Elastic OpenTelemetry ingestion (core):** receives traces, metrics and logs from the Docker services over OTLP, batches them, and sends them to Elastic Cloud. This creates one standard ingestion path and lets us add a differently instrumented target system later without rewriting Faultline.
- **Elasticsearch Query DSL (core):** runs the exact, structured queries behind `incident_timeline`, `clone_vs_production`, `experiment_history`, and `similar_incidents`. Time, environment, incident and clone filters make every result reproducible and keep production evidence separate from clone data and hidden benchmark-controller state.
- **ES|QL (demo):** produces readable, bounded time-series summaries for the CLI and chart, such as DB p99, QPS and retry ratio over one incident. Its pipe-based syntax makes the analysis visibly inspectable by judges and demonstrates that Elastic is doing analysis rather than acting as a JSON bucket.
- **Kibana Discover (demo support):** saved views expose raw OTLP signals alongside the C1 and C4 indices. This gives the distributed-systems owner a quick ingestion check and lets judges inspect the evidence behind a Faultline decision.
- **`semantic_text` plus hybrid search (polish; never verdict input):** searches separately indexed human-readable log-highlight templates, audit details and incident summaries using both exact terms and semantic similarity. It helps a human find related incidents when wording differs, while the Brain's diagnosis remains based only on measured C1 evidence and the noise model.

**OTel plan.** Python auto-instrumentation (FastAPI, httpx, asyncpg) in the shared app image via env, not code changes; `faultctl` and `control` are **not** instrumented. One `otel-collector` service per compose project (production and each clone) with an OTLP receiver, a `resource` processor setting `deployment.environment=production|clone-<slot>`, a `filter` processor dropping `/internal/*` and `/admin/*` spans, exporting to Elastic Cloud's OTLP endpoint with the API key. Owner 2 writes the collector config and env; Owner 1 lands it in `sandbox/` and re-runs two `sweep_lab.py` cells (rps 80, World A + B) to confirm the storm still ignites and heals with instrumentation on. Data streams: Elastic's default `traces-*`/`metrics-*`/`logs-*`; the Kibana APM service map is the demo view.

**Credentials and environment.** The Elastic Cloud deployment runs on sponsor credits. Endpoint + API key live in the root `.env` as `FAULTLINE_ELASTICSEARCH_URL` / `FAULTLINE_ELASTICSEARCH_API_KEY` (auto-loaded by the CLI and the smoke script; `.env` is gitignored, `.env.example` is the template). The Collector reads the same two values. The local `faultline-es` container on `:9200` with no key is the offline fallback; **the demo laptop keeps it running as a hot spare.** Smoke: `cd faultline/telemetry && uv run python scripts/es_smoke.py`.

**Faultline:**

| Component | Responsibility |
| --- | --- |
| Telemetry adapter | `/stats` deltas → per-window C1 fingerprint (p50/p99, QPS, errors, retry ratio, DB query time), persisted to `faultline-fingerprints`; ES Query DSL + ES\|QL for history, similarity and the timeline |
| Detector | SLO threshold alert |
| Triage | OpenAI call: fingerprint → hypotheses + predictions (schema above) |
| Planner + judge | Noise model, lever ranking, support update, confirmation check |
| Action adapter | Envoy admin/config, config endpoints, compose commands; each with undo |
| Code adapter | Devin API session, poll, pull branch, build v2, canary |
| Clone adapter | C6 client: create/reset/destroy clones, run lab actions, replay workload |
| Investigators | One agent per hypothesis running the investigator loop in its clone |
| Orchestrator | State machine for the 8 stages; launches clone investigations; routes Devin patches through clone verification; audit log in Elasticsearch |
| CLI | `faultline watch`, `investigate`, `experiment`, `report` |
| UI | Latency/load chart, hypotheses + evidence panels, planner candidate table, audit log, live investigator/clone panels |

**Fairness rules:** fault-controller state, world labels and trigger timing are never visible to Faultline or its investigators; clones are built only from observable/configurable state; C6 actions only reach clones. On the OTel Demo, filter flagd attributes out of telemetry and never use flag flips as levers. OTel resource and span attributes never carry world/fault labels, `io_profile`, or trigger timing; the collector drops fault-controller and admin spans.

## Demo

The demo is built around one live chart of **DB query latency and request load over time**; no one touches the keyboard between the incident and the report.

1. **Hook (15 s).** "At least 4 of AWS's 15 biggest outages in a decade were metastable failures. The dashboard can't tell you whether your DB is broken or drowning."
2. **Healthy system,** then the hidden incident hits; everything turns red.
3. **Triage says ambiguous.** Storm vs. degraded DB, both plausible, shown in the reasoning panel.
4. **Two clean clones appear.** Investigator A injects a transient DB hiccup and reproduces the production fingerprint; investigator B injects persistent DB degradation and also reproduces it. Each then measures its world's response to a 20 s retry cap: A heals and stays healed, B snaps back.
5. **Planner picks the retry cap** because the clones measured it as the gentlest probe that separates the worlds, not because the LLM guessed.
6. **The chart moment (production).** Cap on: load and latency drop. Cap off: load returns, **latency stays low**, matching clone A. Diagnosis: self-sustaining storm, confirmed; mitigation already in place.
7. **Contrast (pre-recorded or second live run).** Same experiment on the degraded-DB world: latency snaps back, matching clone B. Same experiment, opposite answers.
8. **Durable fix.** Devin patch → fresh clone runs the replay suite, then an investigator tries to break the patch (bigger hiccup, 2× load) → survives → canary at 5% → green → promoted. One line in the report: "this recipe is now in the replay suite."
9. **Morning report + audit log.** Then the benchmark table.

Requirements: record a clean full run as soon as the storm is reliable; rehearse the live run at least 5 times; keep the pre-recorded run as fallback. Show the CLI in Warp for one step.

**Pitch vocabulary.** Use the flashy words, each with the qualifier that makes it ours: *fork production* (no production data, only versions/config/rate); *investigators crawl the clones hunting for the outage* (they must reproduce it, or the theory dies); *aimed chaos* / *chaos with a hypothesis* (every fault is a bet two theories disagree on); *educated chaos against the patch* (Faultline tries to break its own fix before shipping it); *the system is stuck in a traffic jam that outlasted the accident* (metastable failure); *it's allowed to say "I don't know"*. Never say *random*, *explore*, or *swarm*. Never say "proved" for a clone result; say "reproduced in a clone, confirmed in production".

Opening: "At least 4 of AWS's 15 biggest outages were traffic jams that outlasted the accident: the trigger was gone, but retries kept the system down. Dashboards can't tell that from a broken database; they look identical. Faultline forks production into disposable clones. Investigator agents crawl each clone hunting for the outage: one injects a DB hiccup, one injects a degraded DB, both recreate the incident. Then aimed chaos: cap retries, clone A heals, clone B relapses. Now we know the one cheap test that separates them, and we run it in production for 20 seconds. Measurement decides, not the LLM. Then Devin writes the fix, and Faultline attacks it in a fresh clone before a single real request sees it."

## Benchmark and evaluation

The headline result: on ambiguous incidents where passive methods are near chance, Faultline's experiments raise diagnosis accuracy, and it correctly reports none-of-the-above and no-incident.

**Why build our own.** Public root-cause benchmarks like RCAEval and ITBench-AA are offline recordings, so they cannot measure acting on a live system. Live benchmarks (SREGym, AIOpsLab) need Kubernetes; SREGym is future work.

| Suite | Incidents | Proves |
| --- | --- | --- |
| Sandbox (required) | \~10 per world: storm, degraded DB, CPU starvation, no-fault; varied trigger size and load; held-out settings | Experiments beat passive diagnosis on ambiguous incidents |
| OpenTelemetry Demo (if ahead) | 10–20 seeded flag incidents incl. lock contention, plus a few combined | Works as a plug-in on a public system we didn't build |

**Arms (identical telemetry input):**

| Arm | Proves |
| --- | --- |
| Passive-only (stage 3, no stage 4) | What existing evidence alone achieves |
| Production experiment only (v5 loop, 4b without 4a) | Active intervention improves diagnosis (**the key comparison**) |
| Clone investigation + production confirmation (v6) | Counterfactual reproduction adds evidence while keeping invasive experiments off production (ablation) |
| LLM-only (LLM picks and judges experiments) | Measurement and judging matter |
| Nearest-centroid on steady-state fingerprints | Simple telemetry classifier baseline |
| Random lever choice | Planner matters |

The headline claim stays passive vs. active diagnosis. The clone arm is an ablation: it should match or beat production-only with fewer or gentler production actions, and it adds reproduction evidence.

**Metrics:** diagnosis accuracy; production actions and user requests affected per diagnosis (clone arm vs. production-only); reproduction success per hypothesis; none-of-the-above rate on the CPU world; false alarms on no-fault runs; mitigation success; time to mitigation; user requests dropped by experiments; consistency across repeated runs (LLM-only vs. Faultline); tokens per incident vs. dumping raw telemetry to the LLM.

**Runtime:** \~2–3 min per incident → run unattended overnight on one machine with code frozen. Report only measured numbers.

## Sponsor plan

We target four primary challenges, each with one visible piece in the demo, plus two low-effort add-ons.

| Challenge | What they judge | What we show | Owner |
| --- | --- | --- | --- |
| Warp: Best Developer Tool | Improving the dev lifecycle (create, modify, test) | Automated debugging and testing for live systems; CLI run in Warp | Product |
| Cognition: Best Use of Devin | Creativity, novelty, polish | Diagnose → Devin patch → canary verify → evidence back to Devin for revision; also use Devin during the build | Product |
| OpenAI | API use + how Codex helped build | API does triage, hypotheses, structured predictions, report; a concrete Codex story from the build log | Brain |
| Elastic: Find the Signal | Elasticsearch turning messy data into insight/action | OTel ingestion into Elastic Cloud; C1/C4 indices with explicit mappings; Query DSL for incident/experiment history; ES\|QL timeline in the CLI; similar-past-incidents search; Kibana APM + Discover as evidence | Telemetry |
| The Token Company | LLM cost savings in the product | Tokens per incident: compressed fingerprint vs. raw telemetry dump | Brain |
| Ramp | Saves time and money | Time to mitigation vs. a human paging loop | Anyone (write-up only) |

- [ ] Start the Codex log now: what Codex wrote, tested or debugged, with timestamps.
- [ ] Check Runpod when it is announced.
- [ ] Skip challenges requiring tech we don't use (SpaceXAI, Deepgram, ElevenLabs, Meta, hardware).
- [ ] Devpost: one paragraph per sponsor naming exactly where their tool appears.

## Scope, team and timeline

Build the core loop to 100% before any layer; the plan assumes 4 people and a Sunday-morning deadline (confirm both).

**Core (must work end to end):** sandbox + Envoy + load generator; storm and degraded-DB worlds; minimal clone lab (2 investigation clones, \~6 C6 primitives, 2 investigators, patch verification in a clone) working by the 2 am freeze but operationally behind the v5 loop; OTel → Elasticsearch; OpenAI triage with structured predictions; planner over retry cap / shed 10% / shed 50% / DB failover; judge vs. noise; mitigation kept; audit log; Devin → canary → verify → revise with prebuilt fallback; CLI; UI chart + panels + planner candidate table; overnight benchmark **on the live sandbox** (not the simulator; `FakeWorld` stays for unit tests and seeds).

**Committed at 15:45 (scope review; every lane had shipped its → 3:30 pm column):**

- **Live-sandbox benchmark runner.** `bench/` currently runs against `FakeWorld` only. The headline table must come from the real stack, through the same surfaces the integration smoke test uses (C5 to inject, `:9901` to act, `/stats` or ES to observe). \~40 incidents × \~3 min fits one overnight run.
- **Clone arm in the benchmark** (the v6 ablation row), once two investigators work.
- **Similar-incident search** in Elasticsearch over stored fingerprints and reproduction recipes; shown at triage ("this looks like incident X, which was a storm") and in the report. Elastic sponsor moment.
- **Devin live in the demo**, API access verified today; the prebuilt fallback patch and a recorded session are still made.
- **Sequencing:** the v5 loop runs end to end on the live sandbox with real OpenAI triage *before* the clone lab takes anyone's time. Target: first live loop by \~6 pm, not 9 pm, since every piece already exists.

**Depth on the storm (if ahead, in this order; all clone-side, none touches the core loop):**

1. **Find the cliff.** Metastable failures have a tipping point. In one clone, binary-search the request rate at which a 20 s hiccup becomes self-sustaining (`workload.rps` × `db_latency`, ~6 runs of ~60 s). Report: *"checkout becomes self-sustaining above N rps with 3 retries; you run at 80; with retries capped at 1 the cliff moves to M."* Turns the diagnosis into a measured system property and the mitigation into a number.
2. **Every outage becomes a test.** Export the reproduction recipe as a runnable check (`faultline replay <incident-id>` against a fresh clone) and attach it to the Devin PR next to the patch.
3. **"Scaling would have made it worse."** Counterfactual in a clone: scale Payments to 2 replicas, replay the incident, show it getting worse; then the retry cap fixing it with no new capacity. Needs Envoy's payments cluster to pick up the second replica and one new C6 primitive (`scale`); try for 30 minutes, drop if Envoy resists.
4. Admission control at measured capacity (`rate_limit {qps}` at the gateway set to the DB capacity the experiment revealed) folded into the durable-fix story.

**If ahead (other):** none-of-the-above probe in the live demo (the smoke test already covers it), easy case, OpenTelemetry Demo suite, richer report, a hot-key world (human-gated shard split shown as a written case).

**Cut:** SREGym, calibrated response library, change attribution, Kubernetes, extra production levers beyond the four, VM snapshots, clone swarms (> 3 clones), full traffic capture.

| Owner | Builds (v5, kept) | Adds in v6 |
| --- | --- | --- |
| 1 Sandbox + storm | Services, Envoy, load generator, fault controller, storm gate | **The lab:** clone runtime (isolated Compose replicas), create/reset/destroy lifecycle, workload/incident replay, reproducible fault states, C6 lab actions (retry/timeout, load, DB latency/capacity, CPU, batch pause, restart/kill), patched-version slot in clones |
| 2 Telemetry + Elastic | OTel, Collector, Elasticsearch, fingerprint queries, audit log store | ✅ Clone id on all telemetry, ✅ per-clone fingerprints, ✅ clone vs. production fingerprint comparison, ✅ experiment-history storage and queries; open: OTel → Elastic Cloud, reproduction-similarity metric, similar-incident search surfaced at triage |
| 3 Brain | OpenAI triage and predictions, noise model, planner, judge, benchmark runner | **The scientist:** investigator agents (one per hypothesis), clone experiment selection, reproduction and falsification scoring, stopping rules, measured predictions for the production probe, clone arm in the benchmark; *v6.1:* reproduction recipes, patch-attack investigator |
| 4 Product | Orchestrator, action and code adapters, Devin + canary, CLI, UI, demo | Launch clone investigations, C6 clone adapter, live investigator panels, send diagnosis + reproduction to Devin, route the patch through clone verification before the production canary; *v6.1:* replay-suite store, planner candidate table in the UI, counterexample back to Devin |

**Principle:** Owner 1 exposes capabilities (C6); Owner 3 decides when and why to use them.

| Time (Sat → Sun) | Milestone |
| --- | --- |
| now → 3:30 pm | Storm gate passes (or fallback chosen); ~~skeleton services traced into Elasticsearch~~ (slipped: OTel → Collector → Elastic Cloud is now the 6 → 9 pm Owner 2 item) |
| 3:30 → 9 pm | Ugly end-to-end loop: incident → triage → experiment → diagnosis. In parallel: C6 contract agreed, clone runtime up (Owner 1) |
| 9 pm → 2 am | Devin + canary, chart UI, Elastic queries, CLI. Clone lab: 2 investigators reproduce both hero hypotheses; patch verification in a clone |
| 2 → 6 am | Freeze; benchmark runs unattended; record fallback video; polish |
| 6 am → deadline | Rehearse demo; Devpost write-ups per sponsor |

**Parallel tracks** (v6 clone-lab work in *italics*; ✅ = done):

| Phase | 1 Sandbox + storm | 2 Telemetry + Elastic | 3 Brain | 4 Product |
| --- | --- | --- | --- | --- |
| **→ 3:30 pm** | ✅ Services, Postgres, load generator; storm gate (5/5). ✅ Also done early: Envoy, retry override, World B, fault controller, v2 slot, CPU-starve world, `sandbox/INTEGRATION.md`. ✅ *C6 clone-lab contract drafted and approved by Owner 3* | Get OTel → Collector → ES running **first**, then the fingerprint query (sandbox `/stats` mapping in `sandbox/INTEGRATION.md`) | OpenAI triage prompt against C1 fixtures; noise model math; *review and approve C6* | Orchestrator state machine on fake adapters; CLI skeleton; Devin API access check |
| **3:30 → 6 pm** | ✅ *Clone runtime: lab manager `:9910` (`sandbox/services/lab`), `clone.override.yml`, `validate_lab.py fairness` 8/8 (no faultctl, clean DB, production `:9900` refuses clone traffic)* | ✅ Live fingerprint adapter, ES store, audit index, ambiguity export. ✅ Elastic Cloud auth (ApiKey), index templates (keyword ids, date windows, double metrics), audit sink wired into the CLI, ES\|QL `incident_timeline`, search-size fix, root `.env` loading, live smoke against ES 8.15. **Now:** stand up OTel Collector → Elastic Cloud in Compose (plan above); join the live-loop run | ✅ Noise, judge, planner, triage wiring, bench runners on `FakeWorld`. **Now:** ambiguity check *result* (centroid + passive LLM on live fingerprints); join the live-loop run | ✅ Orchestrator, CLI, sandbox + live-telemetry adapters, Devin adapter, canary flow. ✅ **First end-to-end v5 loop on the live sandbox** (`integration/live_loop.py`, PR #16): storm → H_meta confirmed (p50 −7.8σ during, −0.15σ after release), degraded → H_db confirmed (p50 flat during, +74σ after) — with fixture triage fallback; detector fixed to a sustained 60 s breach. **Now:** same run with `OPENAI_API_KEY` set (real triage); verify Devin API access |
| **6 → 9 pm** | ✅ *C6 API checks 24/24 (`validate_lab.py api`); reproduction recipes documented in `INTEGRATION.md`; production + 2 clones profiled (~300 MB, ~0.2 CPU each)*. **Now:** support Owners 3/4 first live clone runs | ✅ *Clone id on all telemetry, per-clone fingerprints (PR #17)*. **Now:** OTel Collector → Elastic Cloud, Kibana APM service map showing production vs. clone; UI data queries | *Investigator loop for one hypothesis on one clone*; **live-sandbox benchmark runner** (C5 + `:9901` + `/stats`/ES; scaffolding exists in `integration/live_loop.py` — reset → inject → run → score → reset) | *C6 clone adapter*; UI chart + two panels + **planner candidate table**; prebuilt fallback patch |
| **9 pm → 2 am** | ✅ *Both hero hypotheses reproduce in clones, CPU world fails both tests (`validate_lab.py storm|degraded|cpu` 25/25); patched orders-v2 builds and canaries in a clone via `patch_ref`.* Remaining: clone reset reliability under repeated runs | **Similar-incident search** over fingerprints + recipes; tokens-per-incident metric; *clone-vs-production similarity metric; experiment-history queries* | *2 investigators, reproduction/falsification scoring, measured predictions for the production probe; reproduction recipes saved; patch-attack investigator*; baselines complete (passive-only, production-only, LLM-only, centroid, random, **clone arm**) | Devin → *replay suite + patch attack in a clone* → build v2 → canary → verify → revise; replay-suite store; *investigator panels*; counterexample back to Devin |
| **2 → 6 am** | Harden storm reliability (✅ 5/5 already); *clone reset reliability* | Support benchmark runs | **Run the overnight benchmark on the live sandbox on frozen code** (*clone arm if stable*) | Report, record fallback video |
| **6 am →** | Rehearse the demo | Elastic Devpost write-up | OpenAI + Token Co write-ups, Codex log | Warp + Devin write-ups, demo driver |

**Cut order if behind:** OTel Demo suite → easy case → none-of-the-above in the live demo (keep it in the benchmark) → `semantic_text`/hybrid search → similar-incident search (keep storage) → patch attack (keep replay) → benchmark clone arm → clone-lab extras (keep 2 investigators) → live-sandbox benchmark (fall back to `FakeWorld`, labelled as simulator numbers) → live canary (show recorded) → Devin live (use fallback patch). If the clone lab threatens the proven storm → experiment → judge loop, fall back to the v5 loop. Never cut: storm, experiment, judge vs. noise, the chart, the benchmark table.

## Definition of done

The project is done when a judge watching the demo can see all thirteen of these, and the build checks below pass. Interface details live in the [Contracts](file/d0142742-f4d4) tab.

**What a judge must see:**

1. Many components look broken at once, and the dashboard alone doesn't say why.
2. Faultline's triage names two plausible causes and says the telemetry can't separate them.
3. The LLM's reasoning and the measured evidence appear as separate panels.
4. The planner picks an experiment for a stated reason: a visible candidate table of separation vs. user impact.
5. The experiment runs live on the incident, and the chart shows the response.
6. The verdict comes from measurement against noise, not an LLM opinion.
7. The diagnosis passes its own confirmation test (stays healthy after the cap is released).
8. The same experiment gives the opposite answer on the degraded-DB world.
9. A Devin patch ships through a canary that Faultline verifies itself.
10. A benchmark table shows experiments beating passive-only and LLM-only on ambiguous incidents.
11. Two investigators each reproduce their hypothesis in a clean clone and measure its response to the retry cap before production is touched.
12. The Devin patch survives the incident replay suite and an investigator's attempt to break it in a clone before its canary.
13. Kibana shows the raw OTel traces of the incident the demo just diagnosed, tagged production vs. clone, next to the C1/C4 indices Faultline wrote.

**Build checks:**

- [x] Storm gate passed: storm persists 60 s+ after trigger ends; retry cap ends it permanently, in 5 of 5 tries.
- [x] Clones start healthy and inherit no hidden state (fairness test); C6 actions cannot reach production (`sandbox/scripts/validate_lab.py fairness`, 8/8).
- [x] Both hero hypotheses reproduce the production fingerprint in clones; CPU starvation reproduces neither (`validate_lab.py storm|degraded|cpu`, 25/25; the CPU world matches on the dashboard but fails both confirmation tests, as in production).
- [ ] Ambiguity check: nearest-centroid and passive LLM near chance on storm vs. degraded DB.
- [x] Full loop runs unattended from incident to report with no manual steps (`integration/live_loop.py`: detect → triage → plan → experiment → verdict → mitigation → patch → report on the live sandbox, both hero worlds; canary stage still refused without `--canary-context`).
- [ ] Every action in the audit log has a recorded undo, and a regression triggers it automatically. *(Undo pairing verified live on both worlds; auto-revert on canary regression is unit-tested only.)*
- [ ] v5 loop ran end to end on the live sandbox with real OpenAI triage (incident → triage → experiment → verdict), before clone work started. *(Ran live with the fixture-triage fallback: storm H_meta confirmed, degraded H_db confirmed. Still needed: the same run with `OPENAI_API_KEY`.)*
- [ ] Benchmark run completed on the live sandbox on frozen code; numbers on slides match the run output and say which arm and how many incidents.
- [ ] Similar-incident search returns the right prior incident for a fresh storm and a fresh degraded-DB run.
- [ ] Fallback video recorded; prebuilt patch works if Devin doesn't return in time.
- [ ] Each primary sponsor's tool is visible at a named moment in the demo.

## Risks, limits and open items

The biggest risk is scope; the second is a storm that won't reproduce reliably.

| Risk | Mitigation |
| --- | --- |
| Too much to build | Core loop first; cut order above |
| Storm doesn't sustain or doesn't break | 3-hour gate; researchers' examples; fallback hero |
| Telemetry separates the worlds passively | Ambiguity check first; move World B's cause somewhere uninstrumented |
| Live demo flakiness | Record clean run early; rehearse 5+ times |
| Looks like another AI-SRE tool | Chart moment is the center of the demo |
| Devin slow or unavailable | Prebuilt fallback patch; show the session separately |
| Clone lab threatens the core loop | Behind the v5 critical path; fall back to v5 at any point |
| Clones overload the demo machine | Production + 2 clones; 3 maximum after profiling |
| Clone reproduction leaks the hidden world | Clones built only from observable state; fairness test; C5 never reachable from investigators |
| Overclaiming clone evidence | In the hero, both hypotheses reproduce; clones measure predictions and falsify, production confirms |
| Non-infra judges | Traffic-jam analogy; chart readable without vocabulary |

**Claims discipline.**

- Say "handles performance and availability incidents," never "handles everything."
- Say "prevents the storm from recurring," not "fixes the root cause," unless the trigger itself was fixed.
- Say "reproduced in a clone," not "proved": a clone is a scaled-down model, and production confirmation is what decides.
- Only measured numbers on slides. Label the sandbox as a scaled-down reproduction.

**Open items:**

- [ ] Confirm submission deadline and team size.
- [ ] Check HackMIT rules on reusing open-source code; credit any reuse.
- [ ] Verify Devin API access with event credentials (today, before 6 pm; decided: Devin live in the demo, fallback recorded).
- [ ] Confirm the OTel Demo runs on a team laptop, or drop it.
- [ ] Confirm the demo machine; profile production + 2 clones on it (dev MacBook: ~300 MB and ~0.2 CPU per healthy clone, fine).
- [x] Agree C6 (clone lab contract) between Owners 1 and 3 before building investigators.
