import type { NodeReading, WorkspaceEvent } from '../model'
import type { ArcScenario, ConfirmSeparatingConfig, EventFactory, ObserverConclusion } from './types'

const ordered = (events: WorkspaceEvent[]) => events.sort((a, b) => a.at - b.at || a.sequence - b.sequence)

function factory(id: string): EventFactory {
  let sequence = 0
  return (at, kind, title, extra = {}) => ({
    id: `${id}-${at}-${kind}-${sequence}`,
    sequence: sequence++,
    at,
    kind,
    title,
    actor: 'orchestrator',
    environmentId: 'production',
    detail: '',
    ...(kind === 'undo' ? { undoStatus: 'undone' as const } : {}),
    ...extra,
  })
}

function observerEvents(make: EventFactory, readings: Record<string, NodeReading>, conclusion: ObserverConclusion): WorkspaceEvent[] {
  return [
    make(11, 'observer', 'Elastic reads the incident window', {
      actor: 'elastic', environmentId: 'elastic', environment: { label: 'Elastic · read-only', color: '#4b5d67' },
      readings, tool: 'elasticsearch.read', detail: 'Elastic read the bounded production metrics and change events available for this incident.',
    }),
    make(conclusion.at, 'reason', conclusion.title, {
      actor: 'elastic', environmentId: 'elastic', diagnosis: conclusion.diagnosis,
      abstained: conclusion.abstained, targetId: conclusion.targetId, evidence: conclusion.evidence,
      hypotheses: conclusion.hypotheses, recommendation: conclusion.recommendation,
      detail: conclusion.detail, tool: 'elastic.conclude',
    }),
  ]
}

export function confirmSeparating(config: ConfirmSeparatingConfig): WorkspaceEvent[] {
  const { scenario, incidentReadings, clones, finalProbe } = config
  const make = factory(scenario.id)
  const events: WorkspaceEvent[] = [
    make(0, 'baseline', 'Healthy reference captured', { actor: 'math', readings: scenario.baseline, tool: 'telemetry.window', phase: 'Monitoring', detail: 'The responder recorded a complete healthy reference for every entity.' }),
    make(10, 'detect', scenario.incidentTitle, { actor: 'math', targetId: scenario.entryId, readings: incidentReadings, tool: 'detector.evaluate', phase: 'Incident detected', detail: config.detectionDetail }),
    ...observerEvents(make, incidentReadings, config.observer),
    make(18, 'reason', 'Faultline keeps two causes alive', { actor: 'model', targetId: scenario.targetId, tool: 'triage.propose', phase: 'Forming hypotheses', detail: config.hypothesisDetail, hypotheses: scenario.hypotheses.map(hypothesis => hypothesis.id) }),
  ]

  clones.forEach((clone, index) => {
    const start = 24 + index * 3
    events.push(
      make(start, 'clone', `${clone.label} tests ${clone.hypothesisTitle}`, {
        actor: clone.actor, environmentId: clone.id, environment: { label: clone.label, color: clone.color, hypothesisId: clone.hypothesisId },
        tool: 'lab.create', phase: index === 0 ? 'Starting clean clones' : undefined,
        args: { source: 'observable config only', workload_rps: scenario.baseline[scenario.entryId]?.qps ?? 80 },
        detail: 'The clone was built from observable topology, versions, retry policy, and workload; no production data or hidden state was copied.',
      }),
      make(start + 2, 'lifecycle', `${clone.label} is ready`, { actor: 'adapter', environmentId: clone.id, lifecycle: 'ready', readings: scenario.baseline, tool: 'lab.ready', detail: 'Readiness checks matched the recorded healthy baseline.' }),
      make(34 + index * 2, 'action', clone.reproduceTitle, {
        actor: clone.actor, environmentId: clone.id, targetId: clone.reproduceTarget, readings: clone.reproduceReadings,
        tool: 'lab.apply', args: { ...clone.reproduceArgs, ttl_s: 14 }, action: { id: `reproduce-${clone.id}`, label: clone.reproduceTitle, ttl: 14 },
        phase: index === 0 ? 'Reproducing both causes' : undefined, detail: clone.reproduceDetail,
      }),
      make(42 + index * 2, 'observe', `${clone.hypothesisTitle} reproduces the symptom`, {
        actor: 'math', environmentId: clone.id, targetId: clone.reproduceTarget, tool: 'evidence.compare',
        detail: 'The clone matched the affected production metrics closely enough to keep this cause in contention.', result: 'Reproduction passed; reproduction alone did not identify the cause.',
      }),
      make(50 + index, 'undo', `${clone.label} reproduction reverted`, { actor: 'adapter', environmentId: clone.id, targetId: clone.reproduceTarget, undoId: `reproduce-${clone.id}`, tool: 'lab.undo', readings: scenario.baseline, detail: 'The reproduction action was reverted and the clone returned to its healthy reference.' }),
      make(56 + index, 'action', clone.probeTitle, {
        actor: clone.actor, environmentId: clone.id, targetId: clone.probeTarget, readings: clone.probeReadings,
        tool: 'lab.apply', args: { ...clone.probeArgs, ttl_s: 8 }, action: { id: `probe-${clone.id}`, label: clone.probeTitle, ttl: 8 },
        phase: index === 0 ? 'Running the separating probe' : undefined, detail: clone.probeDetail,
      }),
      make(64 + index, 'observe', `${clone.label} records the probe response`, {
        actor: 'math', environmentId: clone.id, targetId: clone.probeTarget, tool: 'evidence.compare', detail: clone.probeResult,
        result: clone.probeResult, ...(config.separation ? { separation: config.separation } : {}),
      }),
      make(66 + index, 'undo', `${clone.label} probe released`, { actor: 'adapter', environmentId: clone.id, targetId: clone.probeTarget, undoId: `probe-${clone.id}`, tool: 'lab.undo', detail: 'The separating probe was reverted on schedule.' }),
    )
  })

  events.push(
    make(70, 'observe', config.disagreementTitle, { actor: 'math', tool: 'judge.separation', phase: 'Responses disagree', detail: config.disagreementDetail, ...(config.separation ? { separation: config.separation } : {}) }),
    make(74, 'action', finalProbe.title, {
      actor: finalProbe.actor, environmentId: finalProbe.environmentId, targetId: finalProbe.targetId,
      tool: finalProbe.environmentId === 'production' ? 'levers.apply' : 'lab.apply', args: finalProbe.args,
      action: finalProbe.action, readings: finalProbe.readings,
      phase: finalProbe.environmentId === 'production' ? 'Confirming in production' : 'Confirming in the clone', detail: finalProbe.detail,
    }),
    make(74 + finalProbe.action.ttl, 'undo', finalProbe.releaseTitle, {
      actor: 'adapter', environmentId: finalProbe.environmentId, targetId: finalProbe.targetId, undoId: finalProbe.action.id,
      tool: finalProbe.environmentId === 'production' ? 'levers.undo' : 'lab.undo', readings: finalProbe.releaseReadings,
      phase: 'Watching after release', detail: finalProbe.releaseDetail,
    }),
    make(88, 'verdict', config.verdictTitle, {
      actor: 'math', diagnosis: config.verdictDiagnosis, confirmed: true, targetId: config.verdictTarget,
      tool: 'judge.confirm', phase: 'Confirmed', detail: config.verdictDetail,
      ...(config.separation ? { separation: config.separation } : {}),
    }),
  )

  clones.forEach((clone, index) => events.push(
    make(94 + index, 'lifecycle', `Removing ${clone.label}`, { actor: 'adapter', environmentId: clone.id, lifecycle: 'destroying', tool: 'lab.destroy.request', phase: 'Cleaning up clones', detail: 'Teardown started after all measurements and reversions were recorded.' }),
    make(98 + index, 'archive', `${clone.label} archived`, { actor: 'adapter', environmentId: clone.id, tool: 'lab.destroy', phase: index === 1 ? 'Investigation complete' : undefined, detail: 'The isolated environment was removed and its evidence retained.' }),
  ))
  return ordered(events)
}

export function noIncident(config: {
  scenario: ArcScenario
  transientReadings: Record<string, NodeReading>
  observer: ObserverConclusion
  transientDetail: string
  gateDetail: string
}): WorkspaceEvent[] {
  const make = factory(config.scenario.id)
  return ordered([
    make(0, 'baseline', 'Healthy reference captured', { actor: 'math', readings: config.scenario.baseline, tool: 'telemetry.window', phase: 'Monitoring', detail: 'The responder recorded a complete healthy reference for every entity.' }),
    make(8, 'observer', 'Elastic reads the traffic window', { actor: 'elastic', environmentId: 'elastic', environment: { label: 'Elastic · read-only', color: '#4b5d67' }, readings: config.scenario.baseline, tool: 'elasticsearch.read', detail: 'Elastic read the bounded service and SLO metrics for the traffic change.' }),
    make(15, 'observe', 'A transient window crosses the raw threshold', { actor: 'math', targetId: config.scenario.entryId, readings: config.transientReadings, tool: 'detector.sample', phase: 'Checking the detection gate', detail: config.transientDetail }),
    make(20, 'observe', 'The sustained detection gate does not trip', { actor: 'math', targetId: config.scenario.entryId, tool: 'detector.evaluate', detail: config.gateDetail, result: 'Only 8 of 12 windows breached the noise-adjusted band; 10 are required.' }),
    make(config.observer.at, 'reason', config.observer.title, { actor: 'elastic', environmentId: 'elastic', diagnosis: config.observer.diagnosis, evidence: config.observer.evidence, hypotheses: config.observer.hypotheses, recommendation: config.observer.recommendation, detail: config.observer.detail, tool: 'elastic.conclude' }),
    make(30, 'observe', 'Traffic and latency return to baseline', { actor: 'math', targetId: config.scenario.entryId, readings: config.scenario.baseline, tool: 'telemetry.window', phase: 'Monitoring', detail: 'The next windows returned inside the baseline noise band without any action, clone, or page.' }),
  ])
}

export function exhaustHypotheses(config: {
  scenario: ArcScenario
  incidentReadings: Record<string, NodeReading>
  observer: ObserverConclusion
  detectionDetail: string
  hypothesisDetail: string
}): WorkspaceEvent[] {
  const { scenario } = config
  const make = factory(scenario.id)
  const events: WorkspaceEvent[] = [
    make(0, 'baseline', 'Healthy reference captured', { actor: 'math', readings: scenario.baseline, tool: 'telemetry.window', phase: 'Monitoring', detail: 'The responder recorded a complete healthy reference for every entity.' }),
    make(10, 'detect', scenario.incidentTitle, { actor: 'math', targetId: scenario.targetId, readings: config.incidentReadings, tool: 'detector.evaluate', phase: 'Incident detected', detail: config.detectionDetail }),
    ...observerEvents(make, config.incidentReadings, config.observer),
    make(18, 'reason', 'Faultline keeps data skew and node pressure alive', { actor: 'model', targetId: scenario.targetId, tool: 'triage.propose', phase: 'Forming hypotheses', hypotheses: scenario.hypotheses.map(hypothesis => hypothesis.id), detail: config.hypothesisDetail }),
  ]
  const clones = [
    { id: 'clone-a', actor: 'investigator-a', hypothesis: scenario.hypotheses[0], color: '#957548' },
    { id: 'clone-b', actor: 'investigator-b', hypothesis: scenario.hypotheses[1], color: '#716b60' },
  ] as const
  clones.forEach((clone, index) => events.push(
    make(24 + index * 3, 'clone', `Test ${clone.hypothesis.title}`, { actor: clone.actor, environmentId: clone.id, environment: { label: `Clone ${index ? 'B' : 'A'}`, color: clone.color, hypothesisId: clone.hypothesis.id }, tool: 'lab.create', phase: index === 0 ? 'Starting clean clones' : undefined, args: { source: 'observable config only', workload_rps: scenario.baseline[scenario.entryId]?.qps ?? 80 }, detail: 'The clone contains observable configuration and synthetic workload only; production keys, records, and hidden state were not copied.' }),
    make(26 + index * 3, 'lifecycle', `Clone ${index ? 'B' : 'A'} is ready`, { actor: 'adapter', environmentId: clone.id, lifecycle: 'ready', readings: scenario.baseline, tool: 'lab.ready', detail: 'Readiness checks matched the healthy reference.' }),
    make(34 + index * 2, 'action', 'Replay the observable workload envelope', { actor: clone.actor, environmentId: clone.id, targetId: scenario.targetId, tool: 'lab.apply', args: { workload_rps: 120, ttl_s: 12 }, action: { id: `replay-${clone.id}`, label: 'Observable workload replay', ttl: 12 }, readings: scenario.baseline, phase: index === 0 ? 'Trying to reproduce the incident' : undefined, detail: 'The investigator replayed the recorded aggregate rate without inventing an unavailable per-key distribution.' }),
    make(42 + index * 2, 'observe', `Clone ${index ? 'B' : 'A'} cannot reproduce the skew`, { actor: 'math', environmentId: clone.id, targetId: scenario.targetId, tool: 'evidence.compare', detail: 'Aggregate load matched, but shard concentration, lag, and worker pressure stayed inside the healthy band.', result: 'Reproduction failed because the causal key distribution is absent from C1 and CloneSpec.' }),
    make(46 + index, 'undo', `Clone ${index ? 'B' : 'A'} workload replay reverted`, { actor: 'adapter', environmentId: clone.id, targetId: scenario.targetId, undoId: `replay-${clone.id}`, tool: 'lab.undo', detail: 'The workload replay was reverted.' }),
    make(52 + index, 'reason', `Investigator ${index ? 'B' : 'A'} abstains`, { actor: clone.actor, environmentId: clone.id, diagnosis: 'abstain', abstained: true, tool: 'investigator.abstain', detail: 'Without reproduction, this investigator has no measured intervention that separates the remaining causes.' }),
  ))
  events.push(
    make(58, 'observe', 'No safe separating probe exists', { actor: 'math', targetId: scenario.targetId, tool: 'planner.separation', phase: 'Hypotheses exhausted', detail: 'C1 has no per-key cardinality, CloneSpec cannot carry production data, and the available reversible levers would move both remaining explanations in the same direction.' }),
    make(64, 'verdict', 'Faultline abstains', { actor: 'math', diagnosis: 'abstain', confirmed: false, targetId: scenario.targetId, tool: 'judge.abstain', phase: 'Abstained', detail: 'Neither hypothesis passed a separating confirmation test, so measurement did not name a cause.' }),
    make(66, 'reason', 'Page a human with the unresolved evidence', { actor: 'orchestrator', targetId: scenario.targetId, tool: 'orchestrator.page_human', phase: 'Human review requested', detail: 'The page includes the bounded telemetry, failed reproduction checks, remaining hypotheses, and the reason no safe separating probe was available.' }),
  )
  clones.forEach((clone, index) => events.push(
    make(70 + index, 'lifecycle', `Removing Clone ${index ? 'B' : 'A'}`, { actor: 'adapter', environmentId: clone.id, lifecycle: 'destroying', tool: 'lab.destroy.request', phase: 'Cleaning up clones', detail: 'Teardown started after the abstention evidence was retained.' }),
    make(74 + index, 'archive', `Clone ${index ? 'B' : 'A'} archived`, { actor: 'adapter', environmentId: clone.id, tool: 'lab.destroy', phase: index === 1 ? 'Investigation complete' : undefined, detail: 'The isolated environment was removed and its evidence retained.' }),
  ))
  return ordered(events)
}

