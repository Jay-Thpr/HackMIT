import type { NodeReading, Scenario } from '../model'
import { confirmSeparating, exhaustHypotheses, noIncident } from './builders'
import { existingTopology } from './topologies'
import { checkoutWithReplica, edgeCheckout, fulfilmentQueue, shardedReads } from './shapes'
import type { ArcScenario, CloneTrack } from './types'

type ExistingScenario = Omit<Scenario, 'events'>

const healthy = (latency: number, qps: number, extra: Partial<NodeReading> = {}): NodeReading => ({ health: 'healthy', latency, qps, errorRate: 0.1, retryRatio: 1, ...extra })
const degraded = (latency: number, qps: number, extra: Partial<NodeReading> = {}): NodeReading => ({ health: 'degraded', latency, qps, errorRate: 12.4, retryRatio: 3.9, utilization: 94, ...extra })

function report(id: string, diagnosis: string | null, confirmed: boolean, productionActions: number, pages = 0): NonNullable<Scenario['report']> {
  return {
    outcome: confirmed ? 'confirmed' : diagnosis === 'no_incident' ? 'no_incident' : 'abstained',
    diagnosis, confirmed, verdictAt: confirmed ? 88 : diagnosis === 'abstain' ? 64 : null,
    patch: null, patchProvider: null, patchRevision: null, verification: null, canary: null,
    mitigationHeld: productionActions ? 'reverted after measurement' : 'not required', productionActions, pages,
    startedAt: `2026-09-20T14:00:00Z`, endedAt: `2026-09-20T14:01:${id.length.toString().padStart(2, '0')}Z`,
  }
}

function base(source: Pick<Scenario, 'topology' | 'baseline'>, values: Pick<ArcScenario, 'id' | 'name' | 'subtitle' | 'incident' | 'incidentTitle' | 'targetId' | 'entryId' | 'policyId' | 'duration' | 'hypotheses'> & { report: NonNullable<Scenario['report']> }): ArcScenario {
  return { ...values, ...source, live: true, complete: true, now: values.duration }
}

function track(values: Omit<CloneTrack, 'color'> & { color?: string }): CloneTrack {
  return { color: values.id === 'clone-a' ? '#957548' : '#716b60', ...values }
}

function stormSevere(commerce: ExistingScenario): Scenario {
  const scenario = base(existingTopology(commerce), {
    id: 'storm-severe', name: 'Severe retry storm', subtitle: 'Elastic resolves first; Faultline confirms', incident: 'FL-101',
    incidentTitle: 'Checkout retries are saturating the request path', targetId: 'orders-api', entryId: 'gateway', policyId: 'orders-api', duration: 100,
    hypotheses: [
      { id: 'H_meta', title: 'Self-sustaining retry storm', description: 'Retries keep offered load elevated after the initiating slowdown has ended.', prediction: 'A retry cap recovers the service and recovery persists after release.', color: '#957548' },
      { id: 'H_db', title: 'Degraded database', description: 'Persistent database latency drives callers to retry.', prediction: 'A retry cap lowers load but database latency remains elevated.', color: '#716b60' },
    ], report: report('storm-severe', 'H_meta', true, 1),
  })
  const incident = { gateway: degraded(310, 82, { retryRatio: 3.9 }), 'orders-api': degraded(460, 318, { retryRatio: 4.0 }), 'primary-db': healthy(18, 318, { utilization: 51, retryRatio: 1 }) }
  const recovered = { gateway: scenario.baseline.gateway, 'orders-api': scenario.baseline['orders-api'], 'primary-db': scenario.baseline['primary-db'] }
  const events = confirmSeparating({
    scenario, incidentReadings: incident,
    detectionDetail: 'Retry ratio rose from 1.0 to 4.0 while database query latency remained at 18 ms, and checkout latency breached the sustained gate.',
    hypothesisDetail: 'The flat database latency strongly supports H_meta, but Faultline still requires a release observation before confirming a self-sustaining loop.',
    observer: { at: 14, title: 'Elastic identifies the retry storm', detail: 'Retry attempts quadrupled while database latency stayed inside its baseline band, a passive signature that separates H_meta from H_db.', diagnosis: 'H_meta', targetId: 'orders-api', evidence: ['svc.orders.retry_ratio', 'db.query_p99_ms'], hypotheses: ['H_meta', 'H_db'], recommendation: 'Cap retries and watch recovery through release.' },
    clones: [
      track({ id: 'clone-a', actor: 'investigator-a', label: 'Clone A · H_meta', hypothesisId: 'H_meta', hypothesisTitle: 'H_meta', reproduceTitle: 'Trigger a bounded retry loop', reproduceDetail: 'A 14-second dependency delay caused retry ratio to reach 3.9 after the delay cleared.', reproduceTarget: 'orders-api', reproduceArgs: { extra_ms: 600 }, reproduceReadings: incident, probeTitle: 'Cap retries in the storm clone', probeDetail: 'The clone caps retries at zero to test whether recovery persists after release.', probeTarget: 'orders-api', probeArgs: { max_retries: 0 }, probeReadings: recovered, probeResult: 'Latency recovered by 6.8σ and stayed inside baseline after the cap was released.' }),
      track({ id: 'clone-b', actor: 'investigator-b', label: 'Clone B · H_db', hypothesisId: 'H_db', hypothesisTitle: 'H_db', reproduceTitle: 'Constrain database capacity', reproduceDetail: 'The bounded capacity reduction reproduced high checkout latency with high database latency.', reproduceTarget: 'primary-db', reproduceArgs: { capacity_qps: 40 }, reproduceReadings: { ...incident, 'primary-db': degraded(430, 80) }, probeTitle: 'Cap retries in the degraded-DB clone', probeDetail: 'The same retry cap lowers issued load without repairing the constrained database.', probeTarget: 'orders-api', probeArgs: { max_retries: 0 }, probeReadings: { 'orders-api': degraded(280, 80, { retryRatio: 1 }), 'primary-db': degraded(410, 80, { retryRatio: 1 }) }, probeResult: 'Issued load fell, but database latency remained 5.9σ above baseline.' }),
    ],
    disagreementTitle: 'The release responses separate H_meta from H_db', disagreementDetail: 'Only the retry-storm clone stayed healthy after the cap was released; the database-constrained clone remained slow.', separation: { z: 6.8, sigma: 1 },
    finalProbe: { environmentId: 'production', actor: 'adapter', title: 'Cap production retries for ten seconds', detail: 'The zero-retry cap is TTL-bound and records an explicit undo before measurement begins.', targetId: 'orders-api', args: { max_retries: 0, ttl_s: 10 }, action: { id: 'production-retry-cap', label: 'Retries capped · 0', ttl: 10 }, readings: recovered, releaseTitle: 'Production retry cap released', releaseDetail: 'Retry policy returned to its recorded value and the next windows stayed within the healthy band.', releaseReadings: recovered },
    verdictTitle: 'Self-sustaining retry storm confirmed', verdictDetail: 'Production recovered under the cap and remained healthy after release, matching H_meta at 6.8σ beyond the noise band.', verdictDiagnosis: 'H_meta', verdictTarget: 'orders-api',
  })
  return { ...scenario, events }
}

function ambiguousPair(): Scenario {
  const scenario = base(checkoutWithReplica, {
    id: 'ambiguous-pair', name: 'Degraded DB with elevated retries', subtitle: 'The release window breaks the tie', incident: 'FL-102',
    incidentTitle: 'Checkout latency and retries rise together', targetId: 'primary-db', entryId: 'gateway', policyId: 'orders-api', duration: 100,
    hypotheses: [
      { id: 'H_meta', title: 'Self-sustaining retry storm', description: 'Retries may be maintaining overload after a transient delay.', prediction: 'Recovery persists after the retry cap is released.', color: '#957548' },
      { id: 'H_db', title: 'Degraded database', description: 'The database may remain slow at ordinary offered load.', prediction: 'Database latency stays high and the storm returns after release.', color: '#716b60' },
    ], report: report('ambiguous-pair', 'H_db', true, 1),
  })
  const incident = { gateway: degraded(620, 82), 'orders-api': degraded(940, 310), 'primary-db': degraded(510, 310, { retryRatio: 1, utilization: 97 }) }
  const loadFalls = { gateway: degraded(420, 80, { retryRatio: 1 }), 'orders-api': degraded(610, 80, { retryRatio: 1 }), 'primary-db': degraded(480, 80, { retryRatio: 1 }) }
  const events = confirmSeparating({
    scenario, incidentReadings: incident,
    detectionDetail: 'Checkout latency, retry ratio, database latency, and issued load rose together; both H_meta and H_db fit the window within the measured noise band.',
    hypothesisDetail: 'The current window cannot reveal whether recovery survives release, so neither passive explanation receives causal priority.',
    observer: { at: 20, title: 'Elastic declines to choose between H_meta and H_db', detail: 'The available windows contain the same correlated rise under both explanations and do not contain the counterfactual release response.', diagnosis: 'abstain', abstained: true, evidence: ['svc.orders.retry_ratio', 'db.query_p99_ms', 'svc.orders.qps'], hypotheses: ['H_meta', 'H_db'], recommendation: 'Run a bounded retry-cap probe and observe the release window.' },
    clones: [
      track({ id: 'clone-a', actor: 'investigator-a', label: 'Clone A · H_meta', hypothesisId: 'H_meta', hypothesisTitle: 'H_meta', reproduceTitle: 'Reproduce a retry-maintained overload', reproduceDetail: 'A transient delay cleared while retry traffic kept the clone saturated.', reproduceTarget: 'orders-api', reproduceArgs: { extra_ms: 600 }, reproduceReadings: incident, probeTitle: 'Cap retries in the storm clone', probeDetail: 'The clone tests whether removing retry amplification produces durable recovery.', probeTarget: 'orders-api', probeArgs: { max_retries: 0 }, probeReadings: { gateway: scenario.baseline.gateway, 'orders-api': scenario.baseline['orders-api'], 'primary-db': scenario.baseline['primary-db'] }, probeResult: 'Latency crossed 3.2σ into the healthy band and remained there after release.' }),
      track({ id: 'clone-b', actor: 'investigator-b', label: 'Clone B · H_db', hypothesisId: 'H_db', hypothesisTitle: 'H_db', reproduceTitle: 'Reproduce persistent database latency', reproduceDetail: 'A TTL-bound database capacity reduction reproduced the same checkout and retry metrics.', reproduceTarget: 'primary-db', reproduceArgs: { capacity_qps: 42 }, reproduceReadings: incident, probeTitle: 'Cap retries in the database clone', probeDetail: 'The identical cap removes amplification without changing database capacity.', probeTarget: 'orders-api', probeArgs: { max_retries: 0 }, probeReadings: loadFalls, probeResult: 'Issued load fell to 80 qps while database latency remained 3.2σ above baseline; overload returned after release.' }),
    ],
    disagreementTitle: 'The release window provides the missing counterfactual', disagreementDetail: 'The storm clone stayed healthy after release; the database clone returned to overload. The measured margin is 3.2σ, just beyond the confirmation boundary.', separation: { z: 3.2, sigma: 1 },
    finalProbe: { environmentId: 'production', actor: 'adapter', title: 'Run the bounded production retry-cap probe', detail: 'The cap lowers issued load for ten seconds while database latency is measured independently.', targetId: 'orders-api', args: { max_retries: 0, ttl_s: 10 }, action: { id: 'production-db-separator', label: 'Retries capped · 0', ttl: 10 }, readings: loadFalls, releaseTitle: 'Release the retry cap and watch recurrence', releaseDetail: 'The original retry policy was restored; database latency stayed high and the retry storm returned in the next window.', releaseReadings: incident },
    verdictTitle: 'Persistent database degradation confirmed', verdictDetail: 'Load fell under the cap while database latency stayed high, then overload returned after release; H_db cleared the 3σ confirmation boundary.', verdictDiagnosis: 'H_db', verdictTarget: 'primary-db',
  })
  return { ...scenario, events }
}

function tenantConfined(platform: ExistingScenario): Scenario {
  const scenario = base(existingTopology(platform), {
    id: 'tenant-confined', name: 'Tenant-confined worker starvation', subtitle: 'A scoped clone probe corrects correlated blame', incident: 'FL-103',
    incidentTitle: 'Tenants c and d are falling behind', targetId: 'worker-2', entryId: 'gateway', policyId: 'worker-2', duration: 100,
    hypotheses: [
      { id: 'H_cpu', title: 'Starved fulfilment worker', description: 'One worker lacks CPU and cannot drain its assigned tenant work.', prediction: 'Raising worker CPU drains backlog while the scoped change is held.', color: '#957548' },
      { id: 'H_hotkey', title: 'Hot database shard', description: 'A concentrated tenant write pattern may be saturating shard-1.', prediction: 'Worker CPU does not clear shard latency or backlog.', color: '#716b60' },
    ], report: report('tenant-confined', 'H_cpu', true, 0),
  })
  const incident = { gateway: healthy(49, 120), 'worker-2': degraded(780, 18, { utilization: 99, resourceMetrics: { oldest_pending_ms: 41000 } }), 'shard-1': degraded(410, 52, { retryRatio: 1, resourceMetrics: { tenant_write_qps: 48 } }), 'kafka-1': degraded(120, 22, { resourceMetrics: { consumer_lag_messages: 8420 } }) }
  const workerRaised = { 'worker-2': healthy(52, 22, { utilization: 61, resourceMetrics: { oldest_pending_ms: 480 } }), 'shard-1': healthy(19, 52, { utilization: 54 }), 'kafka-1': healthy(11, 22, { resourceMetrics: { consumer_lag_messages: 36 } }) }
  const events = confirmSeparating({
    scenario, incidentReadings: incident,
    detectionDetail: 'Global checkout SLOs moved less than one sigma, but tenant c and d backlog, worker-2 CPU, kafka-1 lag, and shard-1 writes rose together.',
    hypothesisDetail: 'The loudest correlated downstream signal is shard-1, while the alternative is starvation on worker-2; correlation alone cannot establish direction.',
    observer: { at: 20, title: 'Elastic attributes the slowdown to shard-1', detail: 'Shard-1 is the loudest correlated datastore signal for the affected tenants, so the read-only conclusion names a hot shard.', diagnosis: 'H_hotkey', targetId: 'shard-1', evidence: ['db.shard-1.query_p99_ms', 'db.shard-1.tenant_write_qps', 'svc.worker-2.oldest_pending_ms'], hypotheses: ['H_hotkey', 'H_cpu'], recommendation: 'Inspect shard-1 tenant writes and increase shard capacity.' },
    clones: [
      track({ id: 'clone-a', actor: 'investigator-a', label: 'Clone A · worker', hypothesisId: 'H_cpu', hypothesisTitle: 'worker starvation', reproduceTitle: 'Constrain worker-2 CPU', reproduceDetail: 'A TTL-bound CPU limit reproduced worker backlog, partition lag, and correlated shard writes.', reproduceTarget: 'worker-2', reproduceArgs: { cpu_limit: 0.25 }, reproduceReadings: incident, probeTitle: 'Raise worker-2 CPU in the clone', probeDetail: 'The scoped CPU change affects one of three workers, a 33% worker-pool blast radius.', probeTarget: 'worker-2', probeArgs: { cpu_limit: 1 }, probeReadings: workerRaised, probeResult: 'Worker backlog and kafka-1 lag drained while the CPU increase was held; shard-1 latency followed them down.' }),
      track({ id: 'clone-b', actor: 'investigator-b', label: 'Clone B · shard', hypothesisId: 'H_hotkey', hypothesisTitle: 'hot shard', reproduceTitle: 'Constrain shard-1 capacity', reproduceDetail: 'A bounded shard capacity reduction reproduced latency and backlog on the same path.', reproduceTarget: 'shard-1', reproduceArgs: { db_capacity: 0.4 }, reproduceReadings: incident, probeTitle: 'Raise worker CPU beside a constrained shard', probeDetail: 'The same worker change tests whether a genuinely constrained shard remains slow.', probeTarget: 'worker-2', probeArgs: { cpu_limit: 1 }, probeReadings: { ...incident, 'worker-2': healthy(55, 22, { utilization: 58 }) }, probeResult: 'Worker CPU headroom increased, but shard latency and partition lag did not drain.' }),
    ],
    disagreementTitle: 'Only worker starvation responds to worker CPU', disagreementDetail: 'The worker-starved clone drains backlog while held; the shard-constrained clone stays degraded under the identical worker change.',
    finalProbe: { environmentId: 'clone-a', actor: 'investigator-a', title: 'Repeat the 33% scoped CPU probe', detail: 'The confirmation repeats only in the clean clone; production receives no action.', targetId: 'worker-2', args: { cpu_limit: 1, ttl_s: 10 }, action: { id: 'clone-worker-confirm', label: 'worker-2 CPU raised', ttl: 10 }, readings: workerRaised, releaseTitle: 'Clone worker CPU restored', releaseDetail: 'The clone returned to its recorded CPU allocation after backlog drainage was measured.', releaseReadings: scenario.baseline },
    verdictTitle: 'Worker-2 starvation confirmed', verdictDetail: 'Raising worker-2 CPU drained the affected backlog while held, and the shard-constrained control did not respond to that intervention.', verdictDiagnosis: 'H_cpu', verdictTarget: 'worker-2',
  })
  return { ...scenario, events }
}

function badDeploy(): Scenario {
  const scenario = base(fulfilmentQueue, {
    id: 'bad-deploy', name: 'Config regression after deploy', subtitle: 'Clone-only restart establishes causality', incident: 'FL-104',
    incidentTitle: 'Outbox throughput drops after worker-v42', targetId: 'worker-2', entryId: 'gateway', policyId: 'worker-2', duration: 100,
    hypotheses: [
      { id: 'H_deploy', title: 'Bad deployed configuration', description: 'worker-v42 loaded a concurrency value that stalls worker-2.', prediction: 'A clone restart under the old configuration recovers throughput.', color: '#957548' },
      { id: 'H_queue', title: 'Independent queue pressure', description: 'Partition lag merely coincides with the deploy.', prediction: 'Restarting the service does not clear partition lag.', color: '#716b60' },
    ], report: report('bad-deploy', 'H_deploy', true, 0),
  })
  const incident = { 'worker-2': degraded(930, 6, { resourceMetrics: { oldest_pending_ms: 36000 } }), 'kafka-1': degraded(160, 22, { resourceMetrics: { consumer_lag_messages: 7200 } }), 'outbox-relay': degraded(180, 64, { resourceMetrics: { outbox_age_ms: 8100 } }) }
  const recovered = { 'worker-2': scenario.baseline['worker-2'], 'kafka-1': scenario.baseline['kafka-1'], 'outbox-relay': scenario.baseline['outbox-relay'] }
  const events = confirmSeparating({
    scenario, incidentReadings: incident,
    detectionDetail: 'Worker throughput dropped and partition lag rose in the first sustained windows after worker-v42 changed max_concurrency from 12 to 1.',
    hypothesisDetail: 'The change event makes H_deploy plausible, but it may coincide with independent queue pressure; production exposes neither restart nor rollback through C3.',
    observer: { at: 20, title: 'Elastic declines to call the deploy causal', detail: 'The deployment timestamp aligns with the first breach, but the recorded windows cannot test whether restart or the prior configuration reverses the symptom.', diagnosis: 'abstain', abstained: true, evidence: ['svc.worker-2.qps', 'svc.kafka-1.consumer_lag_messages'], hypotheses: ['H_deploy', 'H_queue'], recommendation: 'Reproduce worker-v42 in isolation and compare a restart under the prior configuration.' },
    clones: [
      track({ id: 'clone-a', actor: 'investigator-a', label: 'Clone A · deploy', hypothesisId: 'H_deploy', hypothesisTitle: 'H_deploy', reproduceTitle: 'Start worker-v42 with max_concurrency=1', reproduceDetail: 'The observable deployed configuration reproduced the throughput collapse and kafka-1 lag.', reproduceTarget: 'worker-2', reproduceArgs: { patch_ref: 'worker-v42', max_concurrency: 1 }, reproduceReadings: incident, probeTitle: 'Restart under the prior configuration', probeDetail: 'C6 service_restart loads max_concurrency=12 in the isolated clone.', probeTarget: 'worker-2', probeArgs: { service_restart: 'worker-2', max_concurrency: 12 }, probeReadings: recovered, probeResult: 'Worker throughput recovered and partition lag drained after the clone-only restart.' }),
      track({ id: 'clone-b', actor: 'investigator-b', label: 'Clone B · queue', hypothesisId: 'H_queue', hypothesisTitle: 'H_queue', reproduceTitle: 'Constrain kafka-1 consumer throughput', reproduceDetail: 'A bounded queue-side constraint reproduced partition lag without the deployed configuration.', reproduceTarget: 'kafka-1', reproduceArgs: { consumer_capacity: 0.35 }, reproduceReadings: incident, probeTitle: 'Restart worker beside constrained queue', probeDetail: 'The identical service restart leaves the independent queue constraint in place.', probeTarget: 'worker-2', probeArgs: { service_restart: 'worker-2', max_concurrency: 12 }, probeReadings: incident, probeResult: 'The worker restart completed, but queue lag remained elevated under the independent constraint.' }),
    ],
    disagreementTitle: 'The clone restart separates deploy from coincidence', disagreementDetail: 'Restarting under the prior configuration repairs the worker-v42 clone, while the independently constrained queue does not respond.',
    finalProbe: { environmentId: 'clone-a', actor: 'investigator-a', title: 'Repeat service_restart in the deploy clone', detail: 'The causal check remains inside C6 because production has no restart or rollback lever.', targetId: 'worker-2', args: { service_restart: 'worker-2', max_concurrency: 12, ttl_s: 10 }, action: { id: 'clone-deploy-confirm', label: 'Restart · prior config', ttl: 10 }, readings: recovered, releaseTitle: 'Clone restart test released', releaseDetail: 'The clone returned to its original isolated configuration after the recovery window was recorded.', releaseReadings: scenario.baseline },
    verdictTitle: 'The deployed configuration regression is confirmed', verdictDetail: 'worker-v42 reproduced the failure and the prior configuration reversed it in the clone; no production probe ran.', verdictDiagnosis: 'H_deploy', verdictTarget: 'worker-2',
  })
  return { ...scenario, events }
}

function benignSpike(): Scenario {
  const scenario = base(edgeCheckout, {
    id: 'benign-spike', name: 'Benign traffic spike', subtitle: 'The sustained gate prevents a false incident', incident: 'FL-105',
    incidentTitle: 'Checkout traffic spikes briefly', targetId: 'gateway', entryId: 'gateway', policyId: 'orders-api', duration: 32,
    hypotheses: [{ id: 'no_incident', title: 'Transient traffic spike', description: 'The service remains inside its sustained noise-adjusted envelope.', prediction: 'The breach clears without intervention.', color: '#4b5d67' }],
    report: report('benign-spike', 'no_incident', false, 0),
  })
  const transient = { gateway: degraded(74, 180, { errorRate: 0.4, retryRatio: 1 }), 'orders-api': degraded(69, 176, { errorRate: 0.3, retryRatio: 1 }), 'primary-db': healthy(21, 176, { utilization: 62 }) }
  const events = noIncident({
    scenario, transientReadings: transient,
    transientDetail: 'Traffic reached 180 qps and latency crossed the raw threshold for 8 windows, while errors and retry ratio stayed near baseline.',
    gateDetail: 'Sigma is the larger of observed standard deviation and 10% of the typical value; only 8 of 12 windows breach it, below the required 10.',
    observer: { at: 24, title: 'Elastic identifies no incident', detail: 'The spike is brief, error rate stays flat, and the following windows return to baseline without intervention.', diagnosis: 'no_incident', evidence: ['svc.gateway.qps', 'svc.gateway.latency_p99_ms', 'slo.checkout_error_rate.value'], hypotheses: ['no_incident'], recommendation: 'Continue monitoring; do not page or change production.' },
  })
  return { ...scenario, events }
}

function hotKey(): Scenario {
  const scenario = base(shardedReads, {
    id: 'hot-key', name: 'Hot shard key', subtitle: 'A stated limit: the cause cannot enter the clone', incident: 'FL-106',
    incidentTitle: 'One shard path is overloaded without a visible cause', targetId: 'shard-1', entryId: 'gateway', policyId: 'worker-2', duration: 76,
    hypotheses: [
      { id: 'H_hotkey', title: 'Hot data-distribution key', description: 'A production key may concentrate writes on shard-1.', prediction: 'The skew reproduces only with the unavailable per-key distribution.', color: '#957548' },
      { id: 'H_node', title: 'Shard node pressure', description: 'Resource pressure on shard-1 may explain the same aggregate metrics.', prediction: 'A clean clone reproduces under the observable aggregate workload.', color: '#716b60' },
    ], report: report('hot-key', 'abstain', false, 0, 1),
  })
  const incident = { gateway: degraded(210, 120, { retryRatio: 1.4 }), 'shard-1': degraded(680, 92, { retryRatio: 1, utilization: 99 }), 'worker-2': degraded(520, 22, { resourceMetrics: { oldest_pending_ms: 28000 } }), 'kafka-1': degraded(140, 22, { resourceMetrics: { consumer_lag_messages: 5900 } }) }
  const events = exhaustHypotheses({
    scenario, incidentReadings: incident,
    detectionDetail: 'Shard-1 latency and utilization rose with worker-2 backlog, but C1 contains only aggregate rates and no per-key cardinality or value distribution.',
    hypothesisDetail: 'H_hotkey and H_node fit the aggregate fingerprint. Copying production records or hidden fault state into either clean clone is forbidden.',
    observer: { at: 20, title: 'Elastic abstains on hot key versus node pressure', detail: 'The read-only evidence shows concentration at shard-1 but lacks the per-key distribution needed to distinguish data skew from node pressure.', diagnosis: 'abstain', abstained: true, evidence: ['db.shard-1.query_p99_ms', 'db.shard-1.qps', 'svc.worker-2.oldest_pending_ms'], hypotheses: ['H_hotkey', 'H_node'], recommendation: 'Collect approved per-key distribution evidence or escalate to a human operator.' },
  })
  return { ...scenario, events }
}

export function buildArcs(commerce: ExistingScenario, platform: ExistingScenario): Scenario[] {
  return [stormSevere(commerce), ambiguousPair(), tenantConfined(platform), badDeploy(), benignSpike(), hotKey()]
}
