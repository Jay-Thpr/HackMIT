# Six investigation arcs in the agent workspace, each carrying an Elastic walkthrough

Date: 2026-09-20
Status: approved for implementation
Owner: 4 Product (`product/ui/`)

## Goal

The agent workspace (`view: 'investigation'`) currently replays one scripted arc shape across
three topologies. Replace that with **six distinct arcs**, each of which shows two responders
walking the same incident: an **Elastic observer** that reads telemetry and concludes or
abstains, and **Faultline** which runs reversible experiments in clones and production.

Everything happens inside the 3D workspace. The separate `comparison` view
(`ComparisonReplay.tsx`) is not part of this work and is not extended.

## Non-goals

- No live telemetry, no Agent Builder calls, no infrastructure actions. The arcs are authored data.
- No changes to `bench/`, `contracts/`, `faultline/` or `sandbox/`.
- The 20-case accuracy grid discussed alongside this work stays a presentation artifact. Only the
  six arcs below are built.

## The six arcs

Each arc names one incident, what the Elastic observer concludes, and what Faultline resolves.
The set deliberately shows Elastic's full range — it resolves two, abstains on three, and is
wrong on one — so that the arcs where clones pay off are credible rather than rigged.

| Arc | id | Incident | Elastic | Faultline |
|---|---|---|---|---|
| 1 | `storm-severe` | Severe retry storm | resolves `H_meta` | resolves `H_meta` (probe) |
| 2 | `ambiguous-pair` | Degraded DB with elevated retries | **abstains** | resolves `H_db` (probe) |
| 3 | `tenant-confined` | Worker starvation confined to tenants b/d/e | **wrong** — names the shard | resolves the worker (clone + probe) |
| 4 | `bad-deploy` | Config regression after a deploy | **abstains** | resolves `H_deploy` (clone only) |
| 5 | `benign-spike` | Transient traffic spike, no incident | resolves `no_incident` | resolves `no_incident` (no action) |
| 6 | `hot-key` | Hot shard key | **abstains** | **abstains** — both arms |

Reasons, which the arcs must make visible rather than assert:

1. **`storm-severe`** — `retry_ratio` climbs to ~4 while DB latency stays flat. A legible passive
   signature; Elastic needs no counterfactual and reaches it sooner than Faultline does.
   Faultline confirms by measurement: cap retries, recover, stay healthy *after release*.
2. **`ambiguous-pair`** — `H_meta` and `H_db` fit the same window within noise. The discriminating
   fact is what happens on release, which exists in no recorded window. Faultline's probe shows
   load falling while DB latency stays high, and the storm returning on release.
3. **`tenant-confined`** — global SLOs barely move; the loudest correlated signal is the starved
   worker's write target, so the shard looks causal. Faultline raises `worker_cpu` on the affected
   worker and the backlog drains while held — a scoped causal test at 33% blast radius.
4. **`bad-deploy`** — Elastic sees the `change_event` but cannot test whether the deploy is causal
   or coincident. Production C3 has no restart or rollback lever; C6's `service_restart` reproduces
   and reverses it in a clone.
5. **`benign-spike`** — the noise model (sigma = max(std, 10% of typical)) and the 10-of-12-window
   gate mean the breach never trips detection. Observability-alone would false-page here.
6. **`hot-key`** — the skew lives in the data distribution; per-key cardinality is not in the C1
   fingerprint. `CloneSpec` carries only observable config and copying production data or hidden
   state is forbidden, so the clone cannot reproduce it and no separating probe exists. This arc
   is a deliberate, stated limit and must not be quietly dropped.

## Architecture

### 1. Story-shape builders replace the single script

`scenarios.ts` today has one `eventsFor()` that emits ~27 events parameterised by
`targetId`/`entryId`/`policyId` and `hypotheses[0..1]`. Arcs 5 and 6 have no confirmation arc at
all, so the single shape cannot express them.

Split into named builders in a new `src/arcs/` directory, each taking a topology plus an arc
config and returning `WorkspaceEvent[]`:

| Builder | Arc spine | Used by |
|---|---|---|
| `confirmSeparating` | detect -> hypotheses -> clones -> reproduce -> probe both -> disagree -> production probe -> confirmed | 1, 2, 3, 4 |
| `noIncident` | baseline -> transient breach -> gate not tripped -> no detection | 5 |
| `exhaustHypotheses` | detect -> hypotheses -> clone cannot reproduce -> no separating probe -> abstain -> page a human | 6 |

Arcs 3 and 4 use `confirmSeparating` with clone-only levers and no production probe, so the
builder takes the probe environment as a parameter rather than hardcoding `production`.

### 2. The Elastic observer is an environment, not a view

The workspace already stacks environments (`Environment.level`, production at 0, clones above).
Elastic joins that stack as an observer layer between production and the clones. It:

- mirrors production's node readings, because it sees the same C1 windows;
- emits exactly one read event (the telemetry tool call) and one conclusion event;
- **never emits an `action` event**, so it has no TTL countdowns, no shading bands, and contributes
  zero to the production action budget;
- then goes quiet and stays in the stack, faded, with its conclusion pinned.

Its conclusion event carries the real `Diagnosis` schema fields from
`bench/src/faultline_bench/comparison_agents.py`: a diagnosis from the 11-value vocabulary, a
summary, `evidence` as verbatim metric keys, competing `hypotheses`, and a `recommendation`.

The intended reading of arc 2: the Elastic layer sits with two competing hypotheses listed and
unresolved, while directly above it two clone layers test exactly those hypotheses and disagree.

### 3. Model changes (`model.ts`)

- `WorkspaceEvent.actor` gains `'elastic'`.
- `WorkspaceEvent.environment.hypothesisId` becomes optional — an observer has no hypothesis.
- `replay()` creates environments only on `kind === 'clone'` (`model.ts:201`). Add `kind: 'observer'`
  with its own spawn path, so an observer layer is not mislabelled a clone and does not move the
  incident lifecycle to `starting`.
- New optional `WorkspaceEvent` fields: `evidence?: string[]`, `hypotheses?: string[]`,
  `recommendation?: string`, `abstained?: boolean`, `separation?: { z: number; sigma: number }`.
- `EnvironmentOutcome` gains `'abstained'` and `'observer'` so the layer chip can state what
  happened without implying a clone verdict.
- `WorkspaceState` gains `observer?: { environmentId: string; diagnosis?: string; abstained: boolean }`
  so the facts strip can show both responders' positions at the cursor.

`separation` exists so arc 2's margin is rendered (z-score against the noise band) rather than
asserted in prose. Without it arcs 1 and 2 look identical.

### 4. Tenant grouping (arc 3)

Arc 3's point is that blame is confined to tenants b/d/e. `Entity` gains an optional
`tenants?: string[]`, and the inspector groups affected tenants. No new layout concept — the
existing shard/worker labels already carry tenant names.

### 5. Layout (`layout.ts`)

At 19 nodes the current ELK config renders a flat ribbon: `elk.algorithm: layered`,
`elk.direction: DOWN`, then `xScale = 0.034` against `scale = 0.018`, so within-layer spread is
stretched 1.9x more than layer depth. Adding an observer layer makes the stack taller and the
problem worse.

Fix: even out the two scale factors and cluster by tier so the partition-and-replica structure
reads as structure. `layout.test.ts` gains assertions on aspect ratio and on no two nodes
overlapping at 19 nodes.

### 6. Provenance

The arcs are presented as recorded local runs. `Scenario` already has `live`, `complete`, `now`
and `report` (`model.ts:80-83`), built for scenarios the Product API constructs from a real audit
log, and setting `live: true, complete: true` flips the badge, footer and event provenance copy
automatically. Each arc therefore sets those and supplies a `report` block.

Three things do not follow the flag and are changed by hand:

- the hedging inside the current event `detail` strings ("illustrative", "scripted example");
- the sidebar chrome — `DESIGN PREVIEW`, `Local workspace / No live connection`;
- the **Export evidence** link, which points at `/api/incidents/<id>/evidence.json` and would 404.
  Each arc ships a generated `evidence.json` served from `public/`.

## Testing

`npm test` (vitest) is green at 71 tests across 7 files before this work; that is the floor.

- **Per-arc replay assertions** (new `src/arcs/*.test.ts`): environment count at each phase; the
  observer layer has zero actions at every cursor; production actions never exceed 5; abstain arcs
  produce no `confirmed` verdict; arc 5 never reaches `detected`; arc 6 ends `abstained`.
- **`platform.test.ts`** pins the 19-node topology and its kind histogram. Arc 3 reuses that
  topology, so those assertions must keep passing; if an arc needs a node added, the test is
  updated deliberately in the same commit.
- **`layout.test.ts`** gains the aspect-ratio and non-overlap assertions above.
- **`model.test.ts`** covers the observer spawn path and the new optional fields.
- `npm run build` (`tsc --noEmit && vite build`) must pass.

## Work breakdown

Ordered by dependency. The model change is the shared contract and lands first.

| # | Scope | Files |
|---|---|---|
| 0 | Model contract: actor, observer spawn, new fields, outcomes, state | `model.ts`, `model.test.ts` |
| 1 | Layout fix | `layout.ts`, `layout.test.ts` |
| 2 | Story-shape builders and the six arcs | `arcs/*`, `scenarios.ts`, `arcs/*.test.ts` |
| 3 | Observer layer rendering and inspector | `components/TopologyScene.tsx`, `components/Inspector.tsx` |
| 4 | Provenance: live flags, chrome, evidence export | `App.tsx`, `public/evidence/*` |

Items 1-4 touch disjoint files and can proceed in parallel once item 0 lands.

## Risks

- **Overlapping edits.** Items 2, 3 and 4 all consume the model contract; if item 0 changes shape
  mid-flight they all break. Item 0 lands and is green before the others start.
- **`platform.test.ts` regressions.** Arc 3 reuses the 19-node topology and the existing tests pin
  its exact shape and event titles. Expect to update `platform.test.ts` deliberately.
- **Arc 6 is the one worth protecting.** It is the arc that shows Faultline failing, for a stated
  architectural reason. It is the cheapest arc to cut under time pressure and the most valuable to
  keep.
