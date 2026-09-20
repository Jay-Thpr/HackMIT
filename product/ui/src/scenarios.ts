import { deriveTopology, type NodeReading, type Scenario, type WorkspaceEvent } from './model'

const healthy = (latency: number, qps: number): NodeReading => ({ health: 'healthy', latency, qps, errorRate: 0.1, retryRatio: 1 })
const degraded = (latency: number, qps: number): NodeReading => ({ health: 'degraded', latency, qps, errorRate: 18.4, retryRatio: 3.9, utilization: 98 })

function eventsFor(scenario: Omit<Scenario, 'events'>): WorkspaceEvent[] {
  const { targetId: target, entryId: entry, policyId: policy, baseline, id } = scenario
  const queue = id === 'pipeline'
  const incident = { [target]: degraded(1240, 312), [policy]: degraded(1680, 312), [entry]: degraded(1820, 80) }
  const make = (at: number, kind: WorkspaceEvent['kind'], title: string, extra: Partial<WorkspaceEvent> = {}): WorkspaceEvent => ({
    id: `${id}-${at}-${kind}`, sequence: at, at, kind, title, actor: 'orchestrator', environmentId: 'production', detail: '', ...extra,
  })
  return [
    make(0, 'baseline', 'Healthy reference captured', { actor: 'math', tool: 'telemetry.window', detail: 'An illustrative healthy reference. No live telemetry is connected.', result: 'Service-level metrics are available; instance inventory is not.', phase: 'Monitoring' }),
    make(12, 'detect', scenario.incidentTitle, { actor: 'math', targetId: entry, readings: incident, tool: 'detector.evaluate', detail: 'Latency and errors have increased across the request path. This identifies symptoms, not the sustaining cause.', phase: 'Incident detected', result: 'Two explanations still fit the observed symptoms.' }),
    make(18, 'reason', 'Two explanations. One observable symptom.', { actor: 'model', targetId: target, phase: 'Forming hypotheses', tool: 'triage.propose', detail: scenario.hypotheses.map(h => h.description).join(' '), prediction: 'If the overload is self-sustaining, limiting retries should allow recovery that persists after release.', result: 'A second possibility is a persistently constrained dependency. Passive telemetry cannot settle the distinction.', causeId: `${id}-12-detect` }),
    make(24, 'clone', 'A clean environment for hypothesis A', { environmentId: 'clone-a', actor: 'investigator-a', environment: { label: 'Clone A', color: '#438c83', hypothesisId: 'A' }, phase: 'Investigating in clones', tool: 'lab.create', args: { workload_rps: 80, source: 'observable config only' }, detail: 'Start from a healthy reference. Do not copy the incident state or production data.' }),
    make(30, 'clone', 'An independent environment for hypothesis B', { environmentId: 'clone-b', actor: 'investigator-b', environment: { label: 'Clone B', color: '#8b78af', hypothesisId: 'B' }, tool: 'lab.create', args: { workload_rps: 80, source: 'observable config only' }, detail: 'Same topology, independently isolated state. The two hypotheses can now be tested in parallel.' }),
    make(35, 'action', queue ? 'Add transient processing latency' : 'Add transient dependency latency', { environmentId: 'clone-a', actor: 'investigator-a', targetId: target, readings: incident, tool: 'lab.apply', args: { extra_ms: 800, ttl_s: 20 }, action: { id: 'perturb-a', label: '+800 ms latency', ttl: 20 }, prediction: 'A temporary slowdown can reproduce sustained overload if retries keep the dependency saturated.', detail: 'Illustrative clone-only perturbation. The adapter would need to report the exact target and a reversible handle.' }),
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
    make(96, 'verdict', 'Self-sustaining overload confirmed', { actor: 'math', targetId: target, tool: 'judge.confirm', phase: 'Confirmed', detail: 'In this scripted example, the production system stays healthy after the probe is released. The hypothesis passes its positive confirmation test.', result: 'This is a simulated outcome, not a live experiment or a benchmark result.', causeId: `${id}-82-undo` }),
    make(105, 'archive', 'Clone A archived; evidence retained', { environmentId: 'clone-a', actor: 'adapter', tool: 'lab.destroy', detail: 'The isolated environment is removed. Its trace remains available.' }),
    make(108, 'archive', 'Clone B archived; evidence retained', { environmentId: 'clone-b', actor: 'adapter', tool: 'lab.destroy', detail: 'No clone state is merged into production.' }),
  ]
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
    { id: 'A', title: 'Self-sustaining retry loop', description: 'A transient slowdown may have ended, while retries keep the dependency overloaded.', prediction: 'Cap retries → latency recovers → stays healthy after release.', color: '#438c83' },
    { id: 'B', title: 'Persistent capacity loss', description: 'The dependency may have insufficient capacity even at the ordinary request rate.', prediction: 'Cap retries → load falls → latency remains high or returns.', color: '#8b78af' },
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
    { id: 'A', title: 'Self-sustaining redelivery', description: 'Retries may be amplifying the event load after the original slowdown has ended.', prediction: 'Limit redelivery → queue delay recovers → remains low after release.', color: '#438c83' },
    { id: 'B', title: 'Constrained consumer capacity', description: 'Consumer capacity may remain below the ordinary event arrival rate.', prediction: 'Limit redelivery → arrivals fall → processing delay remains high.', color: '#8b78af' },
  ],
}

export const scenarios: Scenario[] = [commerce, pipeline].map(scenario => ({ ...scenario, events: eventsFor(scenario) }))
