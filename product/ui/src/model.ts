export type NodeKind = 'service' | 'datastore' | 'queue' | 'external'
export type Health = 'healthy' | 'degraded' | 'unknown'
export type EnvironmentLifecycle = 'unknown' | 'starting' | 'ready' | 'investigating' | 'destroying' | 'archived'
export type EnvironmentOutcome = 'confirmed' | 'ruled-out' | 'fix-verified' | 'fix-superseded' | 'fix-failed'
export type IncidentLifecycle = 'monitoring' | 'detected' | 'starting' | 'investigating' | 'confirming' | 'cleanup' | 'complete'
export type Position = [number, number, number]

export interface Entity {
  id: string
  label: string
  kind: NodeKind
  instrumented: boolean
  instances?: number
}

export interface Relationship {
  id: string
  source: string
  target: string
}

export interface Topology {
  nodes: Entity[]
  edges: Relationship[]
}

export interface NodeReading {
  health: Health
  qps?: number
  latency?: number
  errorRate?: number
  retryRatio?: number
  utilization?: number
}

export interface Environment {
  id: string
  label: string
  color: string
  hypothesisId?: string
  createdAt: number
  lifecycle: EnvironmentLifecycle
  lifecycleAt: number
  level: number
  outcome?: EnvironmentOutcome  // once the verdict exists: which clone carried the confirmed cause / the verified fix
  nodes: Record<string, NodeReading>
}

export interface WorkspaceEvent {
  id: string
  sequence: number
  at: number
  kind: 'baseline' | 'detect' | 'reason' | 'clone' | 'lifecycle' | 'action' | 'observe' | 'undo' | 'verdict' | 'archive'
  actor: 'model' | 'math' | 'adapter' | 'investigator-a' | 'investigator-b' | 'orchestrator'
  environmentId: string
  title: string
  detail: string
  targetId?: string
  phase?: string
  lifecycle?: EnvironmentLifecycle
  incident?: IncidentLifecycle  // live scenarios state the incident phase explicitly (e.g. the report event)
  causeId?: string
  tool?: string
  args?: Record<string, string | number>
  prediction?: string
  result?: string
  diagnosis?: string
  confirmed?: boolean
  action?: { id: string; label: string; ttl: number }
  testResult?: { caseId?: string; checkId: string; passed: boolean; expected: string; observed: string }
  undoId?: string
  undoStatus?: 'active' | 'undone' | 'expired' | 'unknown'
  environment?: { label: string; color: string; hypothesisId: string }
  readings?: Record<string, NodeReading>
}

export interface Scenario {
  id: string
  live?: boolean  // built from a real audit log by the Product API, not a scripted example
  complete?: boolean  // live only: the run has written its report (false while it is still happening)
  now?: number  // live only: wall-clock position on the replay axis when the API built this
  report?: {  // live only: what the audit log recorded for stages 5-8
    outcome: string
    diagnosis: string | null
    confirmed: boolean
    verdictAt: number | null
    patch: string | null
    patchProvider: string | null
    patchRevision: number | null
    verification: string | null
    canary: string | null
    mitigationHeld: string | null
    productionActions: number
    pages: number
    startedAt: string
    endedAt: string
  }
  memory?: {  // retrieval only: prior incidents surfaced at triage, never verdict input
    incidentId: string
    score: number
    diagnosis: string | null
    confirmed: boolean
    recordedAt: number
  }[]
  name: string
  subtitle: string
  incident: string
  incidentTitle: string
  targetId: string
  entryId: string
  policyId: string
  duration: number
  testCases?: { id: string; groupId: 'baseline' | 'reproduction' | 'probe' | 'release'; name: string; description: string }[]
  topology: Topology
  baseline: Record<string, NodeReading>
  hypotheses: { id: string; title: string; description: string; prediction: string; color: string }[]
  // Read-only Elasticsearch retrieval context. These labels never participate
  // in the current incident's verdict.
  similarIncidents?: { incident_id: string; score: number; diagnosis?: string | null; confirmed?: boolean | null; matching_metrics?: string[]; recipe?: string }[]
  events: WorkspaceEvent[]
}

export interface ActiveAction {
  id: string
  label: string
  environmentId: string
  targetId?: string
  start: number
  ttl: number
  status: 'active' | 'release-failed' | 'awaiting-reversion' | 'reverted'
}

export interface WorkspaceState {
  environments: Environment[]
  actions: ActiveAction[]
  phase: string
  lifecycle: IncidentLifecycle
  cleanup: 'not-started' | 'in-progress' | 'complete'
  winner?: string  // environment to emphasise at the end: the verified-fix clone, else the confirmed cause's clone
  verdict?: string
  diagnosis?: string
  confirmed?: boolean
}

export function deriveTopology(input: {
  services: Record<string, unknown>
  edges: { src: string; dst: string }[]
}, hints: Record<string, Partial<Pick<Entity, 'kind' | 'label' | 'instances'>>> = {}): Topology {
  const ids = new Set([...Object.keys(input.services), ...input.edges.flatMap(edge => [edge.src, edge.dst])])
  const nodes = [...ids].sort().map(id => ({
    id,
    label: hints[id]?.label ?? id,
    kind: hints[id]?.kind ?? (Object.hasOwn(input.services, id) ? 'service' : 'external'),
    instrumented: Object.hasOwn(input.services, id),
    ...(hints[id]?.instances === undefined ? {} : { instances: hints[id].instances }),
  }))
  const edges = [...new Map(input.edges.map(edge => {
    const id = JSON.stringify([edge.src, edge.dst])
    return [id, { id, source: edge.src, target: edge.dst }]
  })).values()].sort((a, b) => a.id.localeCompare(b.id))
  return { nodes, edges }
}

export function visibleEvents(scenario: Scenario, time: number): WorkspaceEvent[] {
  return scenario.events.filter(event => event.at <= time).sort((a, b) => a.at - b.at || a.sequence - b.sequence)
}

export function isConfirmedUndo(event: WorkspaceEvent): boolean {
  return event.kind === 'undo' && (event.undoStatus === 'undone' || event.undoStatus === 'expired')
}

export function isConfirmedVerdict(event: WorkspaceEvent): boolean {
  return event.kind === 'verdict' && event.actor === 'math' && event.confirmed === true && Boolean(event.diagnosis) && event.diagnosis !== 'none_of_the_above'
}

export function diagnosisSummary(scenario: Scenario, workspace: WorkspaceState): string {
  if (!workspace.verdict) return 'The cause is not confirmed yet.'
  if (!workspace.confirmed) return 'No cause confirmed.'
  const hypothesis = scenario.hypotheses.find(item => item.id === workspace.diagnosis)
  return `Confirmed cause: ${hypothesis?.title ?? workspace.diagnosis}.`
}

export function replay(scenario: Scenario, time: number): WorkspaceState {
  const environments: Environment[] = [{ id: 'production', label: 'Production', color: '#806747', createdAt: 0, lifecycle: 'ready', lifecycleAt: 0, level: 0, nodes: structuredClone(scenario.baseline) }]
  const actions: ActiveAction[] = []
  let phase = 'Monitoring'
  let lifecycle: IncidentLifecycle = 'monitoring'
  let cleanup: WorkspaceState['cleanup'] = 'not-started'
  let nextLevel = 1
  let verdict: string | undefined
  let diagnosis: string | undefined
  let confirmed: boolean | undefined
  for (const event of visibleEvents(scenario, time)) {
    if (event.phase) phase = event.phase
    const explicit = event.incident !== undefined  // live scenarios state the incident phase; derived transitions below defer to it
    if (event.kind === 'detect' && !explicit) lifecycle = 'detected'
    if (event.kind === 'clone' && event.environment && !environments.some(env => env.id === event.environmentId)) {
      environments.push({ id: event.environmentId, ...event.environment, createdAt: event.at, lifecycle: event.lifecycle ?? (scenario.live ? 'unknown' : 'starting'), lifecycleAt: event.at, level: nextLevel++, nodes: Object.fromEntries(scenario.topology.nodes.map(node => [node.id, { health: 'unknown' }])) })
      if (cleanup !== 'not-started') cleanup = 'in-progress'  // a later clone (sequential investigators, patch verification) after an earlier teardown
      if (!explicit && (lifecycle === 'monitoring' || lifecycle === 'detected' || lifecycle === 'cleanup' || lifecycle === 'complete')) lifecycle = 'starting'
    }
    const environment = environments.find(env => env.id === event.environmentId)
    if (environment && event.readings) environment.nodes = { ...environment.nodes, ...structuredClone(event.readings) }
    if (environment && event.lifecycle) {
      environment.lifecycle = event.lifecycle
      environment.lifecycleAt = event.at
      if (event.lifecycle === 'destroying') { cleanup = 'in-progress'; if (!explicit) lifecycle = 'cleanup' }
    }
    if (event.kind === 'action' && event.action) {
      actions.push({ ...event.action, start: event.at, environmentId: event.environmentId, targetId: event.targetId, status: 'active' })
      if (environment && environment.id !== 'production') { environment.lifecycle = 'investigating'; environment.lifecycleAt = event.at }
      if (explicit) { /* stated by the event */ }
      else if (event.environmentId === 'production') lifecycle = 'confirming'  // real runs destroy the investigation clones before probing production
      else if (cleanup === 'not-started' || lifecycle === 'starting') lifecycle = 'investigating'
    }
    if (event.kind === 'undo') {
      const action = actions.find(item => item.id === event.undoId && item.environmentId === event.environmentId)
      if (action) action.status = isConfirmedUndo(event) ? 'reverted' : event.undoStatus === 'active' ? 'release-failed' : 'awaiting-reversion'
    }
    if (event.kind === 'verdict' && event.environmentId === 'production') {
      verdict = event.title
      diagnosis = event.diagnosis
      confirmed = isConfirmedVerdict(event)
    }
    if (event.kind === 'archive') {
      // The clone project is gone, but the environment stays in the workspace, faded, so the
      // investigation can be reviewed at the end; its evidence and test columns remain.
      const archived = environments.find(env => env.id === event.environmentId && env.id !== 'production')
      if (archived) {
        archived.lifecycle = 'archived'
        archived.lifecycleAt = event.at
        cleanup = environments.every(env => env.id === 'production' || env.lifecycle === 'archived') ? 'complete' : 'in-progress'
        if (!explicit) lifecycle = cleanup === 'complete' ? 'complete' : 'cleanup'
      }
    }
    if (explicit) lifecycle = event.incident!
  }
  for (const action of actions) {
    if (action.status !== 'reverted' && time >= action.start + action.ttl) action.status = 'awaiting-reversion'
  }
  let winner: string | undefined
  if (diagnosis && confirmed) {
    const shown = visibleEvents(scenario, time)
    for (const env of environments) {
      if (env.id === 'production') continue
      if (env.hypothesisId === 'patch') {
        const replays = shown.filter(e => e.environmentId === env.id && e.testResult)
        if (replays.length) env.outcome = replays.every(e => e.testResult!.passed) ? 'fix-verified' : 'fix-failed'
      } else if (env.hypothesisId) env.outcome = env.hypothesisId === diagnosis ? 'confirmed' : 'ruled-out'
    }
    // the latest verified fix wins (an earlier revision may have passed its clone and then failed the canary)
    const verified = environments.filter(env => env.outcome === 'fix-verified')
    for (const env of verified.slice(0, -1)) env.outcome = 'fix-superseded'
    winner = verified.at(-1)?.id ?? environments.find(env => env.outcome === 'confirmed')?.id
  }
  return { environments, actions, phase, lifecycle, cleanup, winner, verdict, diagnosis, confirmed }
}

export const environmentLifecycleLabel: Record<EnvironmentLifecycle, string> = { unknown: 'Readiness not recorded', starting: 'Starting', ready: 'Ready', investigating: 'Investigating', destroying: 'Removing', archived: 'Archived' }
export const environmentOutcomeLabel: Record<EnvironmentOutcome, string> = { confirmed: 'Confirmed cause', 'ruled-out': 'Ruled out', 'fix-verified': 'Fix verified', 'fix-superseded': 'Earlier revision · superseded', 'fix-failed': 'Fix failed' }

/** Archived clones stay in the scene at this presence so the investigation can be reviewed;
 *  the clone that carried the confirmed cause or the verified fix stays at full presence. */
export const ARCHIVED_PRESENCE = 0.4

export function environmentPresence(environment: Environment, cursor: number, reducedMotion = false): number {
  if (environment.id === 'production') return 1
  const emphasised = environment.outcome === 'confirmed' || environment.outcome === 'fix-verified'
  const rest = emphasised ? 1 : ARCHIVED_PRESENCE
  if (environment.lifecycle === 'archived') return rest
  if (reducedMotion) return 1
  if (environment.lifecycle === 'destroying') {
    const progress = Math.max(0, Math.min(1, 1 - (cursor - environment.lifecycleAt) / 3))
    return rest + (1 - rest) * progress * progress * (3 - 2 * progress)
  }
  const progress = Math.max(0, Math.min(1, cursor - environment.createdAt))
  return progress * progress * (3 - 2 * progress)
}

export function metricLabel(value: number | undefined, unit: string): string {
  if (value === undefined || !Number.isFinite(value)) return 'Not collected'
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value)}${unit === '%' ? '' : ' '}${unit}`
}

export function timeLabel(time: number): string {
  return `${String(Math.floor(time / 60)).padStart(2, '0')}:${String(Math.floor(time % 60)).padStart(2, '0')}`
}
