export type NodeKind = 'service' | 'datastore' | 'queue' | 'external'
export type Health = 'healthy' | 'degraded' | 'unknown'
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
  nodes: Record<string, NodeReading>
}

export interface WorkspaceEvent {
  id: string
  sequence: number
  at: number
  kind: 'baseline' | 'detect' | 'reason' | 'clone' | 'action' | 'observe' | 'undo' | 'verdict' | 'archive'
  actor: 'model' | 'math' | 'adapter' | 'investigator-a' | 'investigator-b' | 'orchestrator'
  environmentId: string
  title: string
  detail: string
  targetId?: string
  phase?: string
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
  const environments: Environment[] = [{ id: 'production', label: 'Production', color: '#806747', createdAt: 0, nodes: structuredClone(scenario.baseline) }]
  const actions: ActiveAction[] = []
  let phase = 'Monitoring'
  let verdict: string | undefined
  let diagnosis: string | undefined
  let confirmed: boolean | undefined
  for (const event of visibleEvents(scenario, time)) {
    if (event.phase) phase = event.phase
    if (event.kind === 'clone' && event.environment && !environments.some(env => env.id === event.environmentId)) {
      environments.push({ id: event.environmentId, ...event.environment, createdAt: event.at, nodes: structuredClone(scenario.baseline) })
    }
    const environment = environments.find(env => env.id === event.environmentId)
    if (environment && event.readings) environment.nodes = { ...environment.nodes, ...structuredClone(event.readings) }
    if (event.kind === 'action' && event.action) {
      actions.push({ ...event.action, start: event.at, environmentId: event.environmentId, targetId: event.targetId, status: 'active' })
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
      const index = environments.findIndex(env => env.id === event.environmentId)
      if (index > 0) environments.splice(index, 1)
    }
  }
  for (const action of actions) {
    if (action.status !== 'reverted' && time >= action.start + action.ttl) action.status = 'awaiting-reversion'
  }
  return { environments, actions, phase, verdict, diagnosis, confirmed }
}

export function metricLabel(value: number | undefined, unit: string): string {
  if (value === undefined || !Number.isFinite(value)) return 'Not collected'
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value)}${unit === '%' ? '' : ' '}${unit}`
}

export function timeLabel(time: number): string {
  return `${String(Math.floor(time / 60)).padStart(2, '0')}:${String(Math.floor(time % 60)).padStart(2, '0')}`
}
