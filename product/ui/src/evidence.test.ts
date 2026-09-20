import { describe, expect, it } from 'vitest'
import { evidenceBundle, evidenceFilename } from './evidence'
import { agentActivity } from './agent-activity'
import { deriveTopology, isObserverEnvironment, replay, type Scenario, type WorkspaceEvent } from './model'

const topology = deriveTopology(
  { services: { gateway: {}, api: {} }, edges: [{ src: 'gateway', dst: 'api' }] },
  { api: { kind: 'datastore' } },
)
const baseline = { gateway: { health: 'healthy' as const, latency: 40 }, api: { health: 'healthy' as const, latency: 20 } }
const event = (at: number, kind: WorkspaceEvent['kind'], extra: Partial<WorkspaceEvent> = {}): WorkspaceEvent =>
  ({ id: `e-${at}-${kind}`, sequence: at, at, kind, title: kind, actor: 'orchestrator', environmentId: 'production', detail: '', ...extra })

const report: NonNullable<Scenario['report']> = {
  outcome: 'confirmed', diagnosis: 'A', confirmed: true, verdictAt: 60, patch: null, patchProvider: null,
  patchRevision: null, verification: null, canary: null, mitigationHeld: null, productionActions: 1,
  pages: 0, startedAt: '2026-09-20T09:00:00Z', endedAt: '2026-09-20T09:02:00Z',
}

const arc: Scenario = {
  id: 'arc', name: 'Arc', subtitle: 'test', incident: 'FL-101', incidentTitle: 'Latency is elevated',
  targetId: 'api', entryId: 'gateway', policyId: 'api', duration: 100, topology, baseline,
  live: true, complete: true, report,
  hypotheses: [{ id: 'A', title: 'A', description: '', prediction: '', color: '#000' }],
  events: [
    event(10, 'observer', { environmentId: 'elastic', actor: 'elastic', environment: { label: 'Elastic · read-only', color: '#4b5d67' } }),
    event(12, 'observe', { environmentId: 'elastic', actor: 'elastic', title: 'Read 240 telemetry windows' }),
    event(20, 'reason', { environmentId: 'elastic', actor: 'elastic', diagnosis: 'abstain', abstained: true, hypotheses: ['H_meta', 'H_db'] }),
    event(40, 'action', { targetId: 'api', action: { id: 'cap', label: 'Retries capped', ttl: 10 } }),
    event(52, 'undo', { targetId: 'api', undoId: 'cap', undoStatus: 'undone' }),
    event(60, 'verdict', { actor: 'math', diagnosis: 'A', confirmed: true, title: 'Confirmed' }),
  ],
}

describe('the evidence a recorded run hands over', () => {
  it('is derived from the replay, so it cannot drift from what is displayed', () => {
    const bundle = evidenceBundle(arc)
    const end = replay(arc, arc.duration)
    expect(bundle.outcome.diagnosis).toBe(end.diagnosis)
    expect(bundle.outcome.confirmed).toBe(end.confirmed)
    expect(bundle.outcome.productionActions).toBe(1)
    expect(bundle.outcome.rollbackFailures).toBe(0)
  })

  it('carries the observer position, including an abstention', () => {
    expect(evidenceBundle(arc).outcome.observer).toEqual({ environmentId: 'elastic', diagnosis: 'abstain', abstained: true })
  })

  it('records the topology, the baseline and every event', () => {
    const bundle = evidenceBundle(arc)
    expect(bundle.topology.nodes.map(node => node.id)).toEqual(['api', 'gateway'])
    expect(bundle.topology.edges).toEqual([{ source: 'gateway', target: 'api' }])
    expect(Object.keys(bundle.baseline)).toHaveLength(2)
    expect(bundle.events).toHaveLength(arc.events.length)
  })

  it('states when the run was recorded, and omits it when unrecorded', () => {
    expect(evidenceBundle(arc).recorded).toEqual({ startedAt: report.startedAt, endedAt: report.endedAt })
    expect(evidenceBundle({ ...arc, report: undefined }).recorded).toBeNull()
  })

  it('serialises to JSON, since that is what the download contains', () => {
    expect(() => JSON.stringify(evidenceBundle(arc))).not.toThrow()
    expect(evidenceBundle(arc).schema_version).toBe('faultline-evidence/1')
  })

  it('names the file after the incident', () => {
    expect(evidenceFilename(arc)).toBe('FL-101-evidence.json')
    expect(evidenceFilename({ ...arc, incident: 'FL–026' })).toBe('FL-026-evidence.json')
  })
})

describe('how the observer layer is presented', () => {
  it('is recognised by its outcome, and production never is', () => {
    const state = replay(arc, 30)
    const elastic = state.environments.find(env => env.id === 'elastic')!
    expect(isObserverEnvironment(elastic)).toBe(true)
    expect(isObserverEnvironment(state.environments[0])).toBe(false)
  })

  it('is described by what it read and concluded, never by a test it is running', () => {
    expect(agentActivity(arc, 'elastic', 15).phase).toBe('Reading telemetry')
    const concluded = agentActivity(arc, 'elastic', 30)
    expect(concluded.name).toBe('Read-only responder')
    expect(concluded.phase).toBe('Declined to name a cause')
    expect(concluded.waiting).toContain('does not separate')
  })

  it('says "Concluded" when it did name a cause', () => {
    const named = { ...arc, events: arc.events.map(e => e.kind === 'reason' ? { ...e, diagnosis: 'H_meta', abstained: undefined } : e) }
    expect(agentActivity(named, 'elastic', 30).phase).toBe('Concluded')
  })

  it('leaves the production agent description untouched', () => {
    expect(agentActivity(arc, 'production', 45).name).toBe('Production agent')
    expect(agentActivity(arc, 'production', 45).phase).toBe('Testing')
  })
})
