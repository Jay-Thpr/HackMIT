# Brain sponsor write-up notes

These are submission-ready notes for the Brain-owned sponsor sections. They are
deliberately evidence-bounded: a claim below is either tied to a source file or
to a benchmark command. Do not replace the pending live-run placeholders with
made-up values.

## OpenAI

Faultline uses the OpenAI API at incident triage, where an LLM receives a
compressed telemetry fingerprint and the safe, reversible experiment menu. It
returns a strict structured `TriageDraft`: possible sustaining causes,
directional predictions for every candidate experiment, and a positive test
that could confirm each hypothesis. The response is checked against the C2 JSON
schema and semantic rules; an invalid answer gets one corrective re-prompt.

The LLM is intentionally not the judge. Faultline's noise model derives a
baseline from healthy telemetry, the planner selects the lowest-blast-radius
experiment that separates the proposals, and the judge promotes a diagnosis
only when the measured response passes that diagnosis's own positive
`confirms_if` test. A failed test produces `none_of_the_above` instead of a
confident guess. This makes the OpenAI contribution inspectable in the UI:
model reasoning appears beside the measured observations that accepted or
rejected it.

Implementation evidence:

- `src/faultline_brain/triage.py` requests `gpt-4.1` with strict structured
  output, validates the response, and reports per-attempt token usage.
- `src/faultline_brain/planner.py`, `noise.py`, and `judge.py` make the action
  and verdict deterministic from telemetry and C3/C4 evidence.
- `product/src/faultline_product/adapters/brain.py` wires that path into the
  live product while retaining an explicit fixture fallback when no API key is
  configured.

## The Token Company

Faultline controls LLM cost by sending a compact C1 fingerprint rather than a
raw telemetry stream. The prompt contains the current window, canonical metric
keys, and a short menu of reversible experiments; it does not include a trace
dump or hidden fault-controller state. The triage client records prompt,
completion, and total tokens for every API attempt through an injectable usage
sink. That lets the final report compare tokens per incident for compressed
fingerprints against the same incident represented as raw telemetry.

The token-saving metric is instrumented but intentionally unfilled until a
live API run has been captured:

| Metric | Value |
| --- | --- |
| Compressed-fingerprint prompt tokens / incident | Pending live run |
| Raw-telemetry prompt tokens / incident | Pending live run |
| Completion tokens / incident | Pending live run |
| Total-token reduction | Pending live run |

Record those values from the `usage_sink` output for the same incident and
model before publishing a percentage reduction.

## Codex build log

| Time (EDT) | Evidence | What changed or was verified |
| --- | --- | --- |
| 2026-09-19 15:09 | `e947014` | Tightened triage prompting so `confirms_if` is a positive test of its own hypothesis; prevented CPU starvation from being incorrectly confirmed as a DB fault. |
| 2026-09-19 15:23 | `44e87e0` | Added deterministic active, passive-only, LLM-only, and random-lever benchmark arms plus the nearest-centroid baseline. |
| 2026-09-19 15:35 | `c23c092` | Added frozen suite orchestration and JSON-ready aggregate reporting, keeping C5 fault injection confined to `bench/`. |
| 2026-09-19 15:36 | `bench/tests` | Rebased the suite on current telemetry work and ran the Brain benchmark test suite: 13 passed. |

Codex was used to implement and test the C2 Brain path, identify the
false-positive confirmation rule, build reproducible benchmark arms, and keep
the benchmark/controller boundary separate from runtime Faultline. The commit
history and tests above are the audit trail for that claim.

## Elastic

Faultline writes its C1 fingerprint windows and C4 audit events into Elasticsearch,
tagged by incident and environment/clone identity. Kibana can therefore show the
raw incident evidence beside the decision trace, while structured searches power
timelines, clone-versus-production comparison, and similar-incident retrieval.
The companion **Faultline Investigation** Agent Builder agent is intentionally
read-only: it may explain those bounded results, but cannot call levers, inspect
fault-controller data, infer hidden worlds, select experiments, or issue the C2
verdict. The deterministic Brain remains the authority for triage and judgement.

Implementation evidence:

- `faultline_brain/elastic_investigation.py` defines the restricted agent and its
  four evidence-only tools.
- `product/src/faultline_product/adapters/live_telemetry.py` and the audit sink
  write the corresponding C1/C4 records to Elasticsearch.

## Warp

Faultline is operated as a short, inspectable CLI loop: detection, a reversible
experiment, measured verdict, clone replay, and canary are emitted as staged
terminal events. In the recorded full run (`live-13`), that path went from a
sustained breach to a canaried fix in **6.5 minutes**. The command is deliberately
safe to interrupt: every production lever has a TTL and its corresponding undo
is recorded in C4. This turns the terminal from a deployment launcher into a
high-signal debugging surface for the operator.

## Cognition / Devin

Faultline turns measured evidence into a durable code-change loop. Devin proposes
a patch; Faultline builds it as orders-v2 in a clone, replays the incident suite,
and permits only a surviving patch to enter a 5% canary. `live-14` demonstrated
the negative path too: Devin PR #23 revision 0 regressed checkout latency at the
canary, Faultline automatically rolled back, returned the measured evidence to
the same session, and accepted revision 1 only after clone verification and a
green canary. The system preserves human control by paging rather than silently
shipping a failed or unavailable revision.

## Ramp

The primary time-saving metric is breach-to-canaried-fix: **6.5 minutes** in
the recorded `live-13` full run. Cost is constrained by compact telemetry
fingerprints rather than raw traces: the real OpenAI triage call in that run used
**3.6k tokens**. Together, those numbers describe the tradeoff the project is
optimizing: a bounded investigation that reduces both incident time and model
context cost, while retaining replay and canary gates before a patch is exposed
to customers.

## Submission checklist

- OpenAI and Token Company: use the sections above; replace only the raw-vs-
  compressed comparison once a paired live usage capture exists.
- Elastic: include the read-only Investigation agent boundary and Kibana evidence
  view; do not represent it as a diagnosis agent.
- Warp: show one live CLI stage from the recorded `live-13` flow.
- Cognition/Devin: show the `live-14` revision-and-rollback evidence, not a
  claim that every generated patch is safe.
- Ramp: cite 6.5 minutes and 3.6k tokens with the `live-13` scope stated.

## Benchmark disclosure

The checked-in suite is reproducible with:

```bash
PYTHONPATH=contracts/src:faultline/brain/src:bench/src \
  /opt/anaconda3/bin/python3.12 -m pytest -q bench/tests
```

Its fixture scenarios validate benchmark mechanics; they are not a substitute
for the required frozen live-sandbox run. Put only the resulting live-run JSON
on slides or in headline accuracy claims. In particular, do not describe
fixture accuracy as a production or public-benchmark result.

## Live-run note (not a controlled benchmark result)

`live-1` is excluded from benchmark claims. The freshly built shared stack was
already in a storm before fault injection, and the Docker stack disappeared
after the run. During the retry cap, `db.query_p50_ms` remained about 1186 ms,
so the current fixture's H_db confirmation path fired. That is useful safety
feedback, not evidence of H_db accuracy: host contention can have the same
response to a retry cap. The prompt now requires a direct recovery experiment
for a claimed capacity/dependency cause, and the judge measures the settled
tail of the during phase so a short recovery is not diluted by the initial
drain. The C2 contract now marks retry-cap-only H_db evidence as diagnostic;
the direct DB-failover recovery test is required for confirmation.
