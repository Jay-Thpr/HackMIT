import { describe, expect, it } from 'vitest'
import { deriveTopology, replay, visibleEvents, metricLabel, type WorkspaceEvent } from './model'
import { scenarios } from './scenarios'

describe('adapter-independent topology', () => {
  it('keeps peer-only destinations and disconnected services without assuming container counts', () => {
    const graph = deriveTopology({ services: { entry: {}, isolated: {} }, edges: [{ src: 'entry', dst: 'vendor' }] })
    expect(graph.nodes.map(node => node.id)).toEqual(['entry', 'isolated', 'vendor'])
    expect(graph.nodes.find(node => node.id === 'vendor')).toMatchObject({ kind: 'external', instrumented: false })
    expect(graph.nodes.every(node => node.instances === undefined)).toBe(true)
  })

  it('preserves cycles and does not guess that database-looking names have collected metrics', () => {
    const graph = deriveTopology({ services: { a: {} }, edges: [{ src: 'a', dst: 'postgres' }, { src: 'postgres', dst: 'a' }] })
    expect(graph.edges).toHaveLength(2)
    expect(graph.nodes.find(node => node.id === 'postgres')?.kind).toBe('external')
  })

  it('uses collision-free directed edge ids', () => {
    const graph = deriveTopology({ services: {}, edges: [{ src: 'a-b', dst: 'c' }, { src: 'a', dst: 'b-c' }] })
    expect(new Set(graph.edges.map(edge => edge.id)).size).toBe(2)
  })

  it('renders missing metrics as unavailable, but preserves true zero', () => {
    expect(metricLabel(undefined, 'ms')).toBe('Not collected')
    expect(metricLabel(0, 'ms')).toBe('0 ms')
    expect(metricLabel(Number.NaN, 'ms')).toBe('Not collected')
  })
})

describe('deterministic simulated replay', () => {
  it('never exposes future clones, actions, or verdicts', () => {
    const state = replay(scenarios[0], 0)
    expect(state.environments.map(env => env.id)).toEqual(['production'])
    expect(state.actions).toHaveLength(0)
    expect(state.verdict).toBeUndefined()
    expect(visibleEvents(scenarios[0], 0).every(event => event.at <= 0)).toBe(true)
  })

  it('creates clean clones instead of copying production incident metrics', () => {
    const state = replay(scenarios[0], 25)
    const production = state.environments.find(env => env.id === 'production')!
    const clone = state.environments.find(env => env.id === 'clone-a')!
    expect(production.nodes[scenarios[0].targetId].health).toBe('degraded')
    expect(clone.nodes[scenarios[0].targetId].health).toBe('healthy')
  })

  it('keeps clone changes out of production and restores earlier state on seek', () => {
    const scenario = scenarios[0]
    const during = replay(scenario, 47)
    expect(during.actions.some(action => action.environmentId === 'clone-a')).toBe(true)
    expect(during.actions.some(action => action.environmentId === 'production')).toBe(false)
    expect(replay(scenario, 0).environments).toHaveLength(1)
    expect(replay(scenario, scenario.duration).verdict).toBeDefined()
  })

  it('does not claim successful reversion just because a TTL elapsed', () => {
    const event: WorkspaceEvent = { id: 'apply', sequence: 1, at: 1, kind: 'action', actor: 'adapter', environmentId: 'production', targetId: 'x', title: 'Apply', detail: '', action: { id: 'ttl', label: 'test', ttl: 5 } }
    const state = replay({ ...scenarios[0], events: [event] }, 10)
    expect(state.actions[0].status).toBe('awaiting-reversion')
  })

  it('orders same-time events by explicit sequence', () => {
    const events: WorkspaceEvent[] = [
      { id: 'later', sequence: 2, at: 1, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'second', detail: '' },
      { id: 'earlier', sequence: 1, at: 1, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'first', detail: '' },
    ]
    expect(replay({ ...scenarios[0], events }, 2).verdict).toBe('second')
  })

  it.each([['H_meta', true], ['H_db', true], ['H_db', false], ['none_of_the_above', false]] as const)('replays the measured diagnosis %s with confirmation %s', (diagnosis, confirmed) => {
    const event: WorkspaceEvent = { id: 'verdict', sequence: 1, at: 20, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'Measured result', detail: '', diagnosis, confirmed }
    const scenario = { ...scenarios[0], live: true, events: [event] }
    expect(replay(scenario, 19).diagnosis).toBeUndefined()
    expect(replay(scenario, 20)).toMatchObject({ diagnosis, confirmed })
    const unconfirmed = { ...event, id: 'later', at: 30, diagnosis: 'none_of_the_above', confirmed: false }
    expect(replay({ ...scenario, events: [event, unconfirmed] }, 30)).toMatchObject({ diagnosis: 'none_of_the_above', confirmed: false })
    expect(replay(scenario, 19).confirmed).toBeUndefined()
  })

  it('does not infer confirmation from a legacy title or a model-authored verdict', () => {
    const event: WorkspaceEvent = { id: 'verdict', sequence: 1, at: 20, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'H_meta confirmed', detail: '' }
    expect(replay({ ...scenarios[0], events: [event] }, 20).confirmed).toBe(false)
    expect(replay({ ...scenarios[0], events: [{ ...event, actor: 'model', diagnosis: 'H_db', confirmed: true }] }, 20).confirmed).not.toBe(true)
  })

  it.each(['active', 'undone', 'expired', 'unknown', undefined] as const)('preserves release status %s and awaits confirmation at TTL', (undoStatus) => {
    const apply: WorkspaceEvent = { id: 'apply', sequence: 1, at: 1, kind: 'action', actor: 'adapter', environmentId: 'production', title: 'Apply', detail: '', action: { id: 'ttl', label: 'test', ttl: 10 } }
    const undo: WorkspaceEvent = { id: 'undo', sequence: 2, at: 5, kind: 'undo', actor: 'adapter', environmentId: 'production', title: 'Release', detail: '', undoId: 'ttl', undoStatus }
    const scenario = { ...scenarios[0], live: true, events: [apply, undo] }
    const released = undoStatus === 'undone' || undoStatus === 'expired'
    expect(replay(scenario, 4).actions[0].status).toBe('active')
    expect(replay(scenario, 5).actions[0].status).toBe(released ? 'reverted' : undoStatus === 'active' ? 'release-failed' : 'awaiting-reversion')
    expect(replay(scenario, 12).actions[0].status).toBe(released ? 'reverted' : 'awaiting-reversion')
    expect(replay({ ...scenario, events: [...scenario.events, { ...undo, id: 'verified', sequence: 3, at: 13, undoStatus: 'undone' }] }, 13).actions[0].status).toBe('reverted')
  })

  it('never releases an action in another environment with the same id', () => {
    const apply: WorkspaceEvent = { id: 'apply', sequence: 1, at: 1, kind: 'action', actor: 'adapter', environmentId: 'production', title: 'Apply', detail: '', action: { id: 'shared', label: 'test', ttl: 10 } }
    const undo: WorkspaceEvent = { id: 'undo', sequence: 2, at: 5, kind: 'undo', actor: 'adapter', environmentId: 'clone-a', title: 'Release', detail: '', undoId: 'shared', undoStatus: 'undone' }
    expect(replay({ ...scenarios[0], events: [apply, undo] }, 5).actions[0].status).toBe('active')
  })

  it('uses the same model for a queue-based, cyclic architecture', () => {
    const state = replay(scenarios[1], 47)
    expect(state.environments).toHaveLength(3)
    expect(scenarios[1].topology.nodes.some(node => node.kind === 'queue')).toBe(true)
    expect(state.environments[0].nodes[scenarios[1].targetId]).toBeDefined()
  })
})
