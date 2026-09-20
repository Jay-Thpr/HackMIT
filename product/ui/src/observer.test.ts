import { describe, expect, it } from 'vitest'
import { deriveTopology, replay, type Scenario, type WorkspaceEvent } from './model'

const topology = deriveTopology({ services: { gateway: {}, api: {} }, edges: [{ src: 'gateway', dst: 'api' }] })
const baseline = { gateway: { health: 'healthy' as const, latency: 40 }, api: { health: 'healthy' as const, latency: 20 } }

function scenario(events: WorkspaceEvent[]): Scenario {
  return {
    id: 'arc', name: 'Arc', subtitle: 'test', incident: 'FL-000', incidentTitle: 'Latency is elevated',
    targetId: 'api', entryId: 'gateway', policyId: 'api', duration: 100, topology, baseline,
    hypotheses: [{ id: 'A', title: 'A', description: '', prediction: '', color: '#000' }],
    events,
  }
}

const event = (at: number, kind: WorkspaceEvent['kind'], extra: Partial<WorkspaceEvent> = {}): WorkspaceEvent =>
  ({ id: `e-${at}-${kind}`, sequence: at, at, kind, title: kind, actor: 'orchestrator', environmentId: 'production', detail: '', ...extra })

const spawn = event(10, 'observer', {
  environmentId: 'elastic', actor: 'elastic',
  environment: { label: 'Elastic · read-only', color: '#4b5d67' },
})

describe('the read-only responder layer', () => {
  it('joins the environment stack without being a clone', () => {
    const state = replay(scenario([spawn]), 20)
    expect(state.environments.map(env => env.id)).toEqual(['production', 'elastic'])
    expect(state.environments[1].outcome).toBe('observer')
    expect(state.environments[1].hypothesisId).toBeUndefined()
  })

  it('does not move the incident lifecycle or the cleanup state', () => {
    const state = replay(scenario([event(5, 'detect'), spawn]), 20)
    expect(state.lifecycle).toBe('detected')  // not 'starting': nothing is being built
    expect(state.cleanup).toBe('not-started')
  })

  it('contributes no actions, so the production budget is untouched', () => {
    const state = replay(scenario([spawn, event(30, 'reason', { environmentId: 'elastic', actor: 'elastic', diagnosis: 'H_meta' })]), 60)
    expect(state.actions).toHaveLength(0)
  })

  it('records a named conclusion with its evidence keys', () => {
    const state = replay(scenario([spawn, event(30, 'reason', {
      environmentId: 'elastic', actor: 'elastic', diagnosis: 'H_meta',
      evidence: ['svc.orders.retry_ratio', 'db.query_p99_ms'], hypotheses: ['H_meta', 'H_db'],
      recommendation: 'Cap retries at the gateway.',
    })]), 60)
    expect(state.observer).toEqual({ environmentId: 'elastic', diagnosis: 'H_meta', abstained: false })
    expect(state.environments[1].outcome).toBe('observer')
  })

  it('records an abstention as an outcome, not as a wrong answer', () => {
    const state = replay(scenario([spawn, event(30, 'reason', {
      environmentId: 'elastic', actor: 'elastic', diagnosis: 'abstain', abstained: true,
      hypotheses: ['H_meta', 'H_db'],
    })]), 60)
    expect(state.observer?.abstained).toBe(true)
    expect(state.environments[1].outcome).toBe('abstained')
    expect(state.confirmed).toBeUndefined()
  })

  it('never satisfies the production verdict, even on a verdict event', () => {
    const state = replay(scenario([spawn, event(40, 'verdict', {
      environmentId: 'elastic', actor: 'elastic', diagnosis: 'H_meta', confirmed: true,
    })]), 60)
    expect(state.verdict).toBeUndefined()
    expect(state.confirmed).toBeUndefined()
    expect(state.observer?.diagnosis).toBe('H_meta')
  })

  it('leaves the observer absent when no layer was recorded', () => {
    expect(replay(scenario([event(5, 'detect')]), 20).observer).toBeUndefined()
  })
})
