import { deriveTopology, type NodeReading, type Scenario, type WorkspaceEvent } from './model'
import { buildArcs } from './arcs'

const healthy = (latency: number, qps: number): NodeReading => ({ health: 'healthy', latency, qps, errorRate: 0.1, retryRatio: 1 })
const degraded = (latency: number, qps: number): NodeReading => ({ health: 'degraded', latency, qps, errorRate: 18.4, retryRatio: 3.9, utilization: 98 })

function eventsFor(scenario: Omit<Scenario, 'events'>): WorkspaceEvent[] {
  const { targetId: target, entryId: entry, policyId: policy, baseline, id } = scenario
  const queue = scenario.topology.nodes.find(node => node.id === target)?.kind === 'queue'
  // Keep whatever resource evidence a node carries (consumer lag, replica lag, queue age) visible
  // while it is degraded, so the inspector does not lose those rows mid-incident.
  const strained = (nodeId: string, reading: NodeReading): NodeReading => {
    const metrics = scenario.incidentMetrics?.[nodeId] ?? baseline[nodeId]?.resourceMetrics
    return metrics ? { ...reading, resourceMetrics: metrics } : reading
  }
  const incident = { [target]: strained(target, degraded(1240, 312)), [policy]: strained(policy, degraded(1680, 312)), [entry]: strained(entry, degraded(1820, 80)) }
  const make = (at: number, kind: WorkspaceEvent['kind'], title: string, extra: Partial<WorkspaceEvent> = {}): WorkspaceEvent => ({
    id: `${id}-${at}-${kind}`, sequence: at, at, kind, title, actor: 'orchestrator', environmentId: 'production', detail: '', ...(kind === 'undo' ? { undoStatus: 'undone' as const } : {}), ...extra,
  })
  const events: WorkspaceEvent[] = [
    make(0, 'baseline', 'Healthy reference captured', { actor: 'math', tool: 'telemetry.window', detail: 'An illustrative healthy reference. No live telemetry is connected.', result: 'Service-level metrics are available; instance inventory is not.', phase: 'Monitoring' }),
    make(12, 'detect', scenario.incidentTitle, { actor: 'math', targetId: entry, readings: incident, tool: 'detector.evaluate', detail: 'Latency and errors have increased across the request path. This identifies symptoms, not the sustaining cause.', phase: 'Incident detected', result: 'Two explanations still fit the observed symptoms.' }),
    make(18, 'reason', 'Two explanations. One observable symptom.', { actor: 'model', targetId: target, phase: 'Forming hypotheses', tool: 'triage.propose', detail: scenario.hypotheses.map(h => h.description).join(' '), prediction: 'If the overload is self-sustaining, limiting retries should allow recovery that persists after release.', result: 'A second possibility is a persistently constrained dependency. Passive telemetry cannot settle the distinction.', causeId: `${id}-12-detect` }),
    make(24, 'clone', 'A clean environment for hypothesis A', { environmentId: 'clone-a', actor: 'investigator-a', environment: { label: 'Clone A', color: '#957548', hypothesisId: 'A' }, phase: 'Starting clones', tool: 'lab.create', args: { workload_rps: 80, source: 'observable config only' }, detail: 'Start from a healthy reference. Do not copy the incident state or production data.' }),
    make(30, 'clone', 'An independent environment for hypothesis B', { environmentId: 'clone-b', actor: 'investigator-b', environment: { label: 'Clone B', color: '#716b60', hypothesisId: 'B' }, tool: 'lab.create', args: { workload_rps: 80, source: 'observable config only' }, detail: 'Same topology, independently isolated state. The two hypotheses can now be tested in parallel.' }),
    make(25, 'lifecycle', 'Clone A is ready for investigation', { environmentId: 'clone-a', actor: 'adapter', lifecycle: 'ready', readings: baseline, tool: 'lab.ready', detail: 'The clean clone has completed its startup check. Healthy baseline measurements are now available.' }),
    make(31, 'lifecycle', 'Clone B is ready for investigation', { environmentId: 'clone-b', actor: 'adapter', lifecycle: 'ready', readings: baseline, tool: 'lab.ready', detail: 'The second clone is independently ready. Its observations remain separate from production.' }),
    make(35, 'action', queue ? 'Add transient processing latency' : 'Add transient dependency latency', { environmentId: 'clone-a', actor: 'investigator-a', targetId: target, phase: 'Investigating in clones', readings: incident, tool: 'lab.apply', args: { extra_ms: 800, ttl_s: 20 }, action: { id: 'perturb-a', label: '+800 ms latency', ttl: 20 }, prediction: 'A temporary slowdown can reproduce sustained overload if retries keep the dependency saturated.', detail: 'Illustrative clone-only perturbation. The adapter would need to report the exact target and a reversible handle.' }),
    make(39, 'action', queue ? 'Constrain consumer throughput' : 'Constrain dependency capacity', { environmentId: 'clone-b', actor: 'investigator-b', targetId: target, readings: incident, tool: 'lab.apply', args: { capacity_qps: 40, ttl_s: 51 }, action: { id: 'perturb-b', label: 'Capacity · 40 qps', ttl: 51 }, prediction: 'Persistent capacity loss should reproduce high latency even at the ordinary request rate.', detail: 'A separate cause in a separate clone. Production remains untouched.' }),
    make(44, 'observe', 'Both reproductions match the incident shape', { environmentId: 'clone-a', actor: 'math', targetId: target, tool: 'evidence.compare', detail: 'Compare the same metric window in production and the clone, with provenance and missing-data coverage.', prediction: 'Latency, issued load, and error rate should move together.', result: 'This example matches on all three displayed metrics. Reproduction alone does not confirm the cause.', causeId: `${id}-35-action` }),
    make(47, 'observe', 'The second hypothesis also remains viable', { environmentId: 'clone-b', actor: 'math', targetId: target, tool: 'evidence.compare', detail: 'The same symptoms can have more than one sustaining cause.', result: 'A production intervention is still needed to distinguish them.', causeId: `${id}-39-action` }),
    make(55, 'undo', 'Transient slowdown reverted', { environmentId: 'clone-a', actor: 'adapter', targetId: target, tool: 'lab.undo', undoId: 'perturb-a', detail: 'Reversion is explicitly confirmed in this example, rather than inferred from the countdown.' }),
    make(58, 'action', 'Test the lowest-impact separating intervention', { environmentId: 'clone-a', actor: 'investigator-a', targetId: policy, phase: 'Comparing interventions', tool: 'levers.apply', args: { max_retries: 0, ttl_s: 8 }, action: { id: 'cap-a', label: 'Retries capped · 0', ttl: 8 }, readings: { [target]: baseline[target], [policy]: baseline[policy], [entry]: baseline[entry] }, prediction: scenario.hypotheses[0].prediction, detail: 'An illustrative retry-cap probe in Clone A. This is a diagnostic test, not a production action.' }),
    make(59, 'action', 'Run the same probe in the second clone', { environmentId: 'clone-b', actor: 'investigator-b', targetId: policy, tool: 'levers.apply', args: { max_retries: 0, ttl_s: 8 }, action: { id: 'cap-b', label: 'Retries capped · 0', ttl: 8 }, readings: { [target]: degraded(860, 80), [policy]: degraded(960, 80) }, prediction: scenario.hypotheses[1].prediction, detail: 'Only change one intervention at a time so that the comparison remains meaningful.' }),
    make(66, 'undo', 'Clone A stays healthy after release', { environmentId: 'clone-a', actor: 'adapter', targetId: policy, tool: 'levers.undo', undoId: 'cap-a', detail: 'The example metrics remain near their healthy reference after retries are restored.', result: 'A separating response, reproduced in a clone — not yet confirmed in production.' }),
    make(67, 'undo', 'Clone B does not recover after release', { environmentId: 'clone-b', actor: 'adapter', targetId: policy, tool: 'levers.undo', undoId: 'cap-b', readings: incident, detail: 'The example dependency remains constrained after retries are restored.', result: 'The two clone responses disagree. A short production probe can now discriminate.' }),
    make(72, 'action', 'Confirm with a reversible production probe', { actor: 'adapter', targetId: policy, phase: 'Confirming in production', tool: 'levers.apply', args: { max_retries: 0, ttl_s: 10 }, action: { id: 'cap-production', label: 'Retries capped · 0', ttl: 10 }, readings: { [target]: baseline[target], [policy]: baseline[policy], [entry]: baseline[entry] }, prediction: 'Recovery that persists after release supports a self-sustaining overload.', detail: 'One simulated production action, with a TTL and registered undo. No real requests are affected.' }),
    make(82, 'undo', 'Production probe released', { actor: 'adapter', targetId: policy, tool: 'levers.undo', undoId: 'cap-production', phase: 'Watching recovery', detail: 'The cap is confirmed reverted. Observe the next windows before declaring a result.' }),
    make(90, 'undo', 'Clone B capacity restored', { actor: 'adapter', environmentId: 'clone-b', targetId: target, tool: 'lab.undo', undoId: 'perturb-b', readings: { [target]: baseline[target], [policy]: baseline[policy], [entry]: baseline[entry] }, detail: 'Cleanup is recorded independently of the diagnosis.' }),
    make(96, 'verdict', 'Self-sustaining overload confirmed', { actor: 'math', diagnosis: 'A', confirmed: true, targetId: target, tool: 'judge.confirm', phase: 'Confirmed', detail: 'In this scripted example, the production system stays healthy after the probe is released. The hypothesis passes its positive confirmation test.', result: 'This is a simulated outcome, not a live experiment or a benchmark result.', causeId: `${id}-82-undo` }),
    make(102, 'lifecycle', 'Removing Clone A', { environmentId: 'clone-a', actor: 'adapter', lifecycle: 'destroying', phase: 'Cleaning up clones', tool: 'lab.destroy.request', detail: 'The investigation has finished using this clone. Removal has started; test results and observations are retained.' }),
    make(105, 'archive', 'Clone A archived; evidence retained', { environmentId: 'clone-a', actor: 'adapter', tool: 'lab.destroy', detail: 'The isolated environment is removed. Its trace remains available.' }),
    make(105, 'lifecycle', 'Removing Clone B', { environmentId: 'clone-b', actor: 'adapter', lifecycle: 'destroying', tool: 'lab.destroy.request', detail: 'Removal of the second clone has started. Production continues to run independently.' }),
    make(108, 'archive', 'Clone B archived; evidence retained', { environmentId: 'clone-b', actor: 'adapter', phase: 'Investigation complete', tool: 'lab.destroy', detail: 'Both clones are removed. Production remains healthy in this simulated example, and all recorded evidence is available for review.' }),
  ]

  for (const [env, offset] of [['clone-a', 0], ['clone-b', 1]] as const) {
    const a = env === 'clone-a'
    const results = [
      { at: a ? 25 : 31, checkId: 'baseline', expected: 'The clean clone matches the healthy reference.', observed: 'Baseline readings match the healthy reference.' },
      { at: a ? 44 : 47, checkId: 'reproduction', expected: 'The perturbation reproduces the incident symptoms.', observed: 'Latency, load, and errors reproduce the incident shape.' },
      { at: 62 + offset, checkId: 'probe', expected: a ? 'Latency recovers while retries are capped.' : 'Latency remains elevated while load falls.', observed: a ? 'Readings returned to the healthy reference during the probe.' : 'Load fell to 80 qps; dependency latency remained at 860 ms.' },
      { at: 69 + offset, checkId: 'release', expected: a ? 'Recovery persists after the cap is released.' : 'Overload returns after the cap is released.', observed: a ? 'Recovery persisted after release in this scripted window.' : 'Overload returned as predicted. This passing assertion does not mean the system is healthy.' },
    ]
    for (const result of results) events.push(make(result.at, 'observe', `${result.checkId} check passed`, { id: `${id}-${env}-${result.checkId}`, sequence: result.at + 100, environmentId: env, actor: 'math', tool: 'suite.evaluate', detail: 'Explicit simulated test result; no live test suite was executed.', testResult: { ...result, passed: true }, result: result.observed }))
  }
  return events.sort((a, b) => a.at - b.at || a.sequence - b.sequence)

}

const commerce: Omit<Scenario, 'events'> = {
  id: 'commerce', name: 'Commerce platform', subtitle: 'Request-driven architecture', incident: 'FL–024', incidentTitle: 'Checkout latency is elevated',
  targetId: 'primary-db', entryId: 'gateway', policyId: 'orders-api', duration: 112,
  topology: deriveTopology({
    services: { gateway: {}, 'catalog-api': {}, 'orders-api': {}, payments: {}, inventory: {} },
    edges: [
      { src: 'gateway', dst: 'catalog-api' }, { src: 'gateway', dst: 'orders-api' },
      { src: 'catalog-api', dst: 'cache' }, { src: 'orders-api', dst: 'payments' },
      { src: 'orders-api', dst: 'inventory' }, { src: 'payments', dst: 'primary-db' },
      { src: 'inventory', dst: 'primary-db' },
    ],
  }, { 'primary-db': { kind: 'datastore', label: 'primary-db' }, cache: { kind: 'datastore' } }),
  baseline: { gateway: healthy(48, 80), 'catalog-api': healthy(24, 34), 'orders-api': healthy(42, 80), payments: healthy(32, 80), inventory: healthy(18, 46), 'primary-db': { ...healthy(16, 80), utilization: 48 }, cache: { ...healthy(3, 34), utilization: 24 } },
  hypotheses: [
    { id: 'A', title: 'Self-sustaining retry loop', description: 'A transient slowdown may have ended, while retries keep the dependency overloaded.', prediction: 'Cap retries → latency recovers → stays healthy after release.', color: '#957548' },
    { id: 'B', title: 'Persistent capacity loss', description: 'The dependency may have insufficient capacity even at the ordinary request rate.', prediction: 'Cap retries → load falls → latency remains high or returns.', color: '#716b60' },
  ],
}

const pipeline: Omit<Scenario, 'events'> = {
  id: 'pipeline', name: 'Event pipeline', subtitle: 'Queue-driven architecture', incident: 'FL–025', incidentTitle: 'Event processing latency is elevated',
  targetId: 'events', entryId: 'ingest-api', policyId: 'workers', duration: 112,
  topology: deriveTopology({
    services: { 'ingest-api': {}, normalizer: {}, workers: {}, scheduler: {}, 'admin-api': {} },
    edges: [
      { src: 'ingest-api', dst: 'normalizer' }, { src: 'normalizer', dst: 'events' },
      { src: 'events', dst: 'workers' }, { src: 'workers', dst: 'events' },
      { src: 'scheduler', dst: 'workers' }, { src: 'workers', dst: 'warehouse' },
      { src: 'workers', dst: 'webhook-provider' },
    ],
  }, { events: { kind: 'queue' }, warehouse: { kind: 'datastore' } }),
  baseline: { 'ingest-api': healthy(32, 80), normalizer: healthy(14, 80), events: { ...healthy(22, 80), utilization: 40 }, workers: healthy(40, 80), scheduler: healthy(6, 10), 'admin-api': { health: 'unknown' }, warehouse: healthy(18, 80), 'webhook-provider': { health: 'unknown' } },
  hypotheses: [
    { id: 'A', title: 'Self-sustaining redelivery', description: 'Retries may be amplifying the event load after the original slowdown has ended.', prediction: 'Limit redelivery → queue delay recovers → remains low after release.', color: '#957548' },
    { id: 'B', title: 'Constrained consumer capacity', description: 'Consumer capacity may remain below the ordinary event arrival rate.', prediction: 'Limit redelivery → arrivals fall → processing delay remains high.', color: '#716b60' },
  ],
}


// A partitioned, replicated fulfilment platform: the shape of the advanced sandbox
// (Kafka brokers, tenant-sharded Postgres with streaming replicas, a consumer group of
// workers behind a transactional outbox). Scripted for illustration - no live telemetry
// is connected, and the readings below are examples rather than measurements.
const platform: Omit<Scenario, 'events'> = {
  id: 'platform', name: 'Fulfilment platform', subtitle: 'Partitioned, replicated architecture',
  incident: 'FL-026', incidentTitle: 'Order fulfilment is falling behind',
  targetId: 'kafka-1', entryId: 'gateway', policyId: 'worker-2', duration: 112,
  topology: deriveTopology({
    services: {
      gateway: {}, 'checkout-api': {}, 'inventory-api': {}, 'outbox-relay': {},
      'worker-1': {}, 'worker-2': {}, 'worker-3': {}, reconciler: {},
      // Brokers, shards and replicas report their own resource statistics, so they are
      // instrumented rather than merely observed. payments-provider stays third-party.
      'kafka-0': {}, 'kafka-1': {}, 'kafka-2': {},
      'shard-0': {}, 'shard-1': {}, 'shard-2': {},
      'shard-0-replica': {}, 'shard-1-replica': {}, 'shard-2-replica': {}, redis: {},
    },
    edges: [
      { src: 'gateway', dst: 'checkout-api' }, { src: 'gateway', dst: 'inventory-api' },
      { src: 'checkout-api', dst: 'redis' }, { src: 'inventory-api', dst: 'redis' },
      { src: 'checkout-api', dst: 'shard-0' }, { src: 'checkout-api', dst: 'shard-1' }, { src: 'checkout-api', dst: 'shard-2' },
      { src: 'inventory-api', dst: 'shard-1' },
      { src: 'shard-0', dst: 'shard-0-replica' }, { src: 'shard-1', dst: 'shard-1-replica' }, { src: 'shard-2', dst: 'shard-2-replica' },
      { src: 'outbox-relay', dst: 'shard-0' }, { src: 'outbox-relay', dst: 'shard-1' }, { src: 'outbox-relay', dst: 'shard-2' },
      { src: 'outbox-relay', dst: 'kafka-0' }, { src: 'outbox-relay', dst: 'kafka-1' }, { src: 'outbox-relay', dst: 'kafka-2' },
      { src: 'kafka-0', dst: 'worker-1' }, { src: 'kafka-1', dst: 'worker-2' }, { src: 'kafka-2', dst: 'worker-3' },
      { src: 'worker-1', dst: 'shard-0' }, { src: 'worker-2', dst: 'shard-1' }, { src: 'worker-3', dst: 'shard-2' },
      { src: 'worker-1', dst: 'payments-provider' }, { src: 'worker-2', dst: 'payments-provider' }, { src: 'worker-3', dst: 'payments-provider' },
      { src: 'reconciler', dst: 'shard-0-replica' }, { src: 'reconciler', dst: 'shard-1-replica' }, { src: 'reconciler', dst: 'shard-2-replica' },
    ],
  }, {
    'checkout-api': { instances: 2 },
    redis: { kind: 'datastore' },
    'kafka-0': { kind: 'queue', label: 'kafka-0 - orders p0,p3' },
    'kafka-1': { kind: 'queue', label: 'kafka-1 - orders p1,p4' },
    'kafka-2': { kind: 'queue', label: 'kafka-2 - orders p2,p5' },
    'shard-0': { kind: 'datastore', label: 'shard-0 - tenants a,b', tenants: ['a', 'b'] },
    'shard-1': { kind: 'datastore', label: 'shard-1 - tenants c,d', tenants: ['c', 'd'] },
    'shard-2': { kind: 'datastore', label: 'shard-2 - tenants e,f', tenants: ['e', 'f'] },
    'shard-0-replica': { kind: 'datastore' }, 'shard-1-replica': { kind: 'datastore' }, 'shard-2-replica': { kind: 'datastore' },
    'worker-1': { tenants: ['a', 'b'] }, 'worker-2': { tenants: ['c', 'd'] }, 'worker-3': { tenants: ['e', 'f'] },
  }),
  baseline: {
    gateway: healthy(46, 120), 'checkout-api': healthy(38, 78), 'inventory-api': healthy(21, 42),
    redis: { ...healthy(2, 120), utilization: 22 },
    'outbox-relay': { ...healthy(12, 64), resourceMetrics: { outbox_age_ms: 180 } },
    'kafka-0': { ...healthy(9, 21), utilization: 34, resourceMetrics: { consumer_lag_messages: 12 } },
    'kafka-1': { ...healthy(9, 22), utilization: 36, resourceMetrics: { consumer_lag_messages: 14 } },
    'kafka-2': { ...healthy(9, 21), utilization: 33, resourceMetrics: { consumer_lag_messages: 11 } },
    'worker-1': { ...healthy(34, 21), resourceMetrics: { oldest_pending_ms: 240 } },
    'worker-2': { ...healthy(35, 22), resourceMetrics: { oldest_pending_ms: 260 } },
    'worker-3': { ...healthy(33, 21), resourceMetrics: { oldest_pending_ms: 230 } },
    'shard-0': { ...healthy(14, 40), utilization: 44 }, 'shard-1': { ...healthy(16, 52), utilization: 51 }, 'shard-2': { ...healthy(14, 39), utilization: 43 },
    'shard-0-replica': { ...healthy(11, 40), utilization: 30, resourceMetrics: { replica_lag_bytes: 4096 } },
    'shard-1-replica': { ...healthy(12, 52), utilization: 34, resourceMetrics: { replica_lag_bytes: 5120 } },
    'shard-2-replica': { ...healthy(11, 39), utilization: 29, resourceMetrics: { replica_lag_bytes: 3584 } },
    reconciler: healthy(8, 6), 'payments-provider': { health: 'unknown' },
  },
  incidentMetrics: {
    'kafka-1': { consumer_lag_messages: 8420 },
    'worker-2': { oldest_pending_ms: 41000 },
    'outbox-relay': { outbox_age_ms: 9800 },
  },
  hypotheses: [
    { id: 'A', title: 'Self-sustaining redelivery', description: 'Failed fulfilments may be redelivered on the same partition faster than the consumer group can drain them, keeping the partition saturated after the original slowdown ended.', prediction: 'Cap redelivery -> partition lag drains -> stays drained after release.', color: '#957548' },
    { id: 'B', title: 'Constrained consumer capacity', description: 'The consumer group assigned to that partition may simply have less capacity than the ordinary arrival rate, independently of retries.', prediction: 'Cap redelivery -> arrivals fall -> lag remains high or returns.', color: '#716b60' },
  ],
}

const arcs = buildArcs(commerce, platform)

/** The three scripted examples built by `eventsFor`. They all share one clone timeline:
 *  detect at 12, clones at 24/30, production probe at 72, verdict at 96, archived by 108.
 *  The six recorded arcs deliberately do not - `benign-spike` never detects and creates no
 *  clone, `hot-key` never confirms - so tests that assert the shared timeline iterate this
 *  list, and the arcs are covered by `arcs/arcs.test.ts`. */
export const uniformTimelineScenarios: Scenario[] = [commerce, pipeline, platform]
  .map(scenario => ({ ...scenario, events: eventsFor(scenario) }))

export const scenarios: Scenario[] = [...uniformTimelineScenarios, ...arcs]
