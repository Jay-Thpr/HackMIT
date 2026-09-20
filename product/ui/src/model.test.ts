import { describe, expect, it } from 'vitest'
import { ARCHIVED_PRESENCE, deriveTopology, environmentPresence, replay, visibleEvents, metricLabel, type WorkspaceEvent } from './model'
import { uniformTimelineScenarios } from './scenarios'

describe('incident lifecycle', () => {
  for (const scenario of uniformTimelineScenarios) {
    it(`${scenario.id}: begins healthy and waits for observed clone readiness`, () => {
      expect(replay(scenario, 0)).toMatchObject({ lifecycle: 'monitoring', cleanup: 'not-started' })
      expect(replay(scenario, 12).lifecycle).toBe('detected')
      const starting = replay(scenario, 24)
      expect(starting.lifecycle).toBe('starting')
      expect(starting.environments[1]).toMatchObject({ lifecycle: 'starting', level: 1, lifecycleAt: 24 })
      expect(starting.environments[1].nodes[scenario.targetId]).toEqual({ health: 'unknown' })
      expect(replay(scenario, 25).environments[1]).toMatchObject({ lifecycle: 'ready', nodes: scenario.baseline })
      expect(replay(scenario, 39).environments.slice(1).every(env => env.lifecycle === 'investigating')).toBe(true)
      expect(replay(scenario, 72).lifecycle).toBe('confirming')
    })

    it(`${scenario.id}: animates cleanup, keeps archived clones for review and emphasises the winner`, () => {
      const cleanup = replay(scenario, 103)
      expect(cleanup).toMatchObject({ lifecycle: 'cleanup', cleanup: 'in-progress' })
      expect(cleanup.environments[1]).toMatchObject({ lifecycle: 'destroying', lifecycleAt: 102, level: 1 })
      const afterA = replay(scenario, 105)
      expect(afterA.environments.map(env => env.id)).toEqual(['production', 'clone-a', 'clone-b'])  // archived clones stay
      expect(afterA.environments[1]).toMatchObject({ lifecycle: 'archived', lifecycleAt: 105, level: 1 })
      expect(afterA.cleanup).toBe('in-progress')
      const done = replay(scenario, 108)
      expect(done).toMatchObject({ lifecycle: 'complete', cleanup: 'complete' })
      expect(done.environments).toHaveLength(3)
      expect(done.environments.every(env => env.id === 'production' || env.lifecycle === 'archived')).toBe(true)
      // the verdict confirmed hypothesis A: its clone is the winner, B is ruled out
      expect(done.environments.find(env => env.id === 'clone-a')?.outcome).toBe('confirmed')
      expect(done.environments.find(env => env.id === 'clone-b')?.outcome).toBe('ruled-out')
      expect(done.winner).toBe('clone-a')
      expect(replay(scenario, 47).winner).toBeUndefined()  // no emphasis before the verdict
      expect(visibleEvents(scenario, 108).some(event => event.testResult)).toBe(true)
      expect(replay(scenario, 47).environments).toHaveLength(3)
      expect(replay(scenario, 47).verdict).toBeUndefined()
      expect(replay(scenario, 0).cleanup).toBe('not-started')
    })

    it(`${scenario.id}: derives presence from the replay clock, not elapsed wall time`, () => {
      const starting = replay(scenario, 24).environments[1]
      expect(environmentPresence(starting, 24)).toBe(0)
      expect(environmentPresence(starting, 24.5)).toBeCloseTo(0.5)
      expect(environmentPresence(starting, 25)).toBe(1)
      expect(environmentPresence(starting, 24.5)).toBeCloseTo(0.5)
      // clone A carries the confirmed cause: it never fades while being removed, and stays at full presence archived
      const removingWinner = replay(scenario, 103).environments[1]
      expect(environmentPresence(removingWinner, 103.5)).toBe(1)
      expect(environmentPresence(replay(scenario, 108).environments[1], 108)).toBe(1)
      // clone B was ruled out: it fades from 1 down to the archived presence, then stays there
      const removingB = replay(scenario, 106).environments[2]
      expect(environmentPresence(removingB, 105)).toBe(1)
      expect(environmentPresence(removingB, 106.5)).toBeCloseTo(ARCHIVED_PRESENCE + (1 - ARCHIVED_PRESENCE) * 0.5)
      expect(environmentPresence(removingB, 108)).toBe(ARCHIVED_PRESENCE)
      expect(environmentPresence(replay(scenario, 108).environments[2], 108)).toBe(ARCHIVED_PRESENCE)
      expect(environmentPresence(removingB, 106.5, true)).toBe(1)
    })
  }

  it('does not remove clones or claim cleanup just because a verdict exists', () => {
    const scenario = { ...uniformTimelineScenarios[0], events: uniformTimelineScenarios[0].events.filter(event => event.kind !== 'archive' && event.lifecycle !== 'destroying') }
    expect(replay(scenario, 112).environments).toHaveLength(3)
    expect(replay(scenario, 112).cleanup).toBe('not-started')
  })
})

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
    const state = replay(uniformTimelineScenarios[0], 0)
    expect(state.environments.map(env => env.id)).toEqual(['production'])
    expect(state.actions).toHaveLength(0)
    expect(state.verdict).toBeUndefined()
    expect(visibleEvents(uniformTimelineScenarios[0], 0).every(event => event.at <= 0)).toBe(true)
  })

  it('creates clean clones instead of copying production incident metrics', () => {
    const state = replay(uniformTimelineScenarios[0], 25)
    const production = state.environments.find(env => env.id === 'production')!
    const clone = state.environments.find(env => env.id === 'clone-a')!
    expect(production.nodes[uniformTimelineScenarios[0].targetId].health).toBe('degraded')
    expect(clone.nodes[uniformTimelineScenarios[0].targetId].health).toBe('healthy')
  })

  it('keeps clone changes out of production and restores earlier state on seek', () => {
    const scenario = uniformTimelineScenarios[0]
    const during = replay(scenario, 47)
    expect(during.actions.some(action => action.environmentId === 'clone-a')).toBe(true)
    expect(during.actions.some(action => action.environmentId === 'production')).toBe(false)
    expect(replay(scenario, 0).environments).toHaveLength(1)
    expect(replay(scenario, scenario.duration).verdict).toBeDefined()
  })

  it('does not claim successful reversion just because a TTL elapsed', () => {
    const event: WorkspaceEvent = { id: 'apply', sequence: 1, at: 1, kind: 'action', actor: 'adapter', environmentId: 'production', targetId: 'x', title: 'Apply', detail: '', action: { id: 'ttl', label: 'test', ttl: 5 } }
    const state = replay({ ...uniformTimelineScenarios[0], events: [event] }, 10)
    expect(state.actions[0].status).toBe('awaiting-reversion')
  })

  it('orders same-time events by explicit sequence', () => {
    const events: WorkspaceEvent[] = [
      { id: 'later', sequence: 2, at: 1, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'second', detail: '' },
      { id: 'earlier', sequence: 1, at: 1, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'first', detail: '' },
    ]
    expect(replay({ ...uniformTimelineScenarios[0], events }, 2).verdict).toBe('second')
  })

  it.each([['H_meta', true], ['H_db', true], ['H_db', false], ['none_of_the_above', false]] as const)('replays the measured diagnosis %s with confirmation %s', (diagnosis, confirmed) => {
    const event: WorkspaceEvent = { id: 'verdict', sequence: 1, at: 20, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'Measured result', detail: '', diagnosis, confirmed }
    const scenario = { ...uniformTimelineScenarios[0], live: true, events: [event] }
    expect(replay(scenario, 19).diagnosis).toBeUndefined()
    expect(replay(scenario, 20)).toMatchObject({ diagnosis, confirmed })
    const unconfirmed = { ...event, id: 'later', at: 30, diagnosis: 'none_of_the_above', confirmed: false }
    expect(replay({ ...scenario, events: [event, unconfirmed] }, 30)).toMatchObject({ diagnosis: 'none_of_the_above', confirmed: false })
    expect(replay(scenario, 19).confirmed).toBeUndefined()
  })

  it('does not infer confirmation from a legacy title or a model-authored verdict', () => {
    const event: WorkspaceEvent = { id: 'verdict', sequence: 1, at: 20, kind: 'verdict', actor: 'math', environmentId: 'production', title: 'H_meta confirmed', detail: '' }
    expect(replay({ ...uniformTimelineScenarios[0], events: [event] }, 20).confirmed).toBe(false)
    expect(replay({ ...uniformTimelineScenarios[0], events: [{ ...event, actor: 'model', diagnosis: 'H_db', confirmed: true }] }, 20).confirmed).not.toBe(true)
  })

  it.each(['active', 'undone', 'expired', 'unknown', undefined] as const)('preserves release status %s and awaits confirmation at TTL', (undoStatus) => {
    const apply: WorkspaceEvent = { id: 'apply', sequence: 1, at: 1, kind: 'action', actor: 'adapter', environmentId: 'production', title: 'Apply', detail: '', action: { id: 'ttl', label: 'test', ttl: 10 } }
    const undo: WorkspaceEvent = { id: 'undo', sequence: 2, at: 5, kind: 'undo', actor: 'adapter', environmentId: 'production', title: 'Release', detail: '', undoId: 'ttl', undoStatus }
    const scenario = { ...uniformTimelineScenarios[0], live: true, events: [apply, undo] }
    const released = undoStatus === 'undone' || undoStatus === 'expired'
    expect(replay(scenario, 4).actions[0].status).toBe('active')
    expect(replay(scenario, 5).actions[0].status).toBe(released ? 'reverted' : undoStatus === 'active' ? 'release-failed' : 'awaiting-reversion')
    expect(replay(scenario, 12).actions[0].status).toBe(released ? 'reverted' : 'awaiting-reversion')
    expect(replay({ ...scenario, events: [...scenario.events, { ...undo, id: 'verified', sequence: 3, at: 13, undoStatus: 'undone' }] }, 13).actions[0].status).toBe('reverted')
  })

  it('never releases an action in another environment with the same id', () => {
    const apply: WorkspaceEvent = { id: 'apply', sequence: 1, at: 1, kind: 'action', actor: 'adapter', environmentId: 'production', title: 'Apply', detail: '', action: { id: 'shared', label: 'test', ttl: 10 } }
    const undo: WorkspaceEvent = { id: 'undo', sequence: 2, at: 5, kind: 'undo', actor: 'adapter', environmentId: 'clone-a', title: 'Release', detail: '', undoId: 'shared', undoStatus: 'undone' }
    expect(replay({ ...uniformTimelineScenarios[0], events: [apply, undo] }, 5).actions[0].status).toBe('active')
  })

  it('uses the same model for a queue-based, cyclic architecture', () => {
    const state = replay(uniformTimelineScenarios[1], 47)
    expect(state.environments).toHaveLength(3)
    expect(uniformTimelineScenarios[1].topology.nodes.some(node => node.kind === 'queue')).toBe(true)
    expect(state.environments[0].nodes[uniformTimelineScenarios[1].targetId]).toBeDefined()
  })
})
