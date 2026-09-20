import { beforeEach, describe, expect, it } from 'vitest'
import { useWorkspace } from './store'

describe('demo playback', () => {
  beforeEach(() => useWorkspace.setState(useWorkspace.getInitialState()))

  it('opens on a paused, healthy system and resets when changing architecture', () => {
    expect(useWorkspace.getState()).toMatchObject({ cursor: 0, playing: false, environmentId: 'production' })
    useWorkspace.getState().seek(47)
    useWorkspace.getState().setScenario('pipeline')
    expect(useWorkspace.getState()).toMatchObject({ cursor: 0, playing: false, follow: false, selectedNode: undefined })
  })

  it('starts a complete walkthrough and clears stale inspection state', () => {
    useWorkspace.getState().inspect('primary-db', 'clone-a')
    useWorkspace.getState().set({ speed: 4, selectedAgent: 'clone-a' })
    useWorkspace.getState().startDemo('pipeline')
    expect(useWorkspace.getState()).toMatchObject({ scenarioId: 'pipeline', cursor: 0, playing: true, follow: true, speed: 1, environmentId: 'production', isolatedLayer: null, selectedNode: undefined, selectedAgent: undefined, view: 'investigation' })
    useWorkspace.getState().tick(1)
    expect(useWorkspace.getState().cursor).toBe(1)
    useWorkspace.getState().togglePlay()
    useWorkspace.getState().tick(10)
    expect(useWorkspace.getState().cursor).toBe(1)
  })

  it('clears removed clone selection when seeking backward and can restart after completion', () => {
    useWorkspace.getState().seek(47)
    useWorkspace.getState().inspect('primary-db', 'clone-a')
    useWorkspace.getState().seek(0)
    expect(useWorkspace.getState()).toMatchObject({ environmentId: 'production', isolatedLayer: null, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined })
    useWorkspace.getState().seek(112)
    useWorkspace.getState().togglePlay()
    expect(useWorkspace.getState()).toMatchObject({ cursor: 0, playing: true })
  })
})

describe('layer focus', () => {
  beforeEach(() => useWorkspace.getState().setScenario('commerce'))

  it('starts with every layer visible, isolates explicit focus, and resets with architecture', () => {
    expect(useWorkspace.getState().isolatedLayer).toBeNull()
    useWorkspace.getState().focus('clone-a')
    expect(useWorkspace.getState()).toMatchObject({ environmentId: 'clone-a', isolatedLayer: 'clone-a', follow: false })
    useWorkspace.getState().setScenario('pipeline')
    expect(useWorkspace.getState()).toMatchObject({ environmentId: 'production', isolatedLayer: null })
  })

  it('node inspection isolates that environment and opens its recorded activity', () => {
    useWorkspace.getState().inspect('primary-db', 'clone-b')
    expect(useWorkspace.getState()).toMatchObject({ isolatedLayer: 'clone-b', environmentId: 'clone-b', selectedNode: 'primary-db', traceTab: 'trace', follow: false })
  })
})

describe('live incidents', () => {
  it('updateScenario follows a playing viewer at the end and leaves a paused or rewound one alone', async () => {
    const { useWorkspace } = await import('./store')
    const base = useWorkspace.getState().scenarios[0]
    const live = { ...base, id: 'inc-1', live: true, complete: false, duration: 100, events: base.events.filter(e => e.at <= 100) }
    useWorkspace.getState().addScenarios([live], 'inc-1')
    useWorkspace.getState().seek(100)
    useWorkspace.getState().updateScenario({ ...live, duration: 140 })
    expect(useWorkspace.getState().cursor).toBe(100)  // seeking pauses; a paused viewer is not dragged forward
    useWorkspace.getState().seek(140)
    useWorkspace.getState().set({ playing: true })
    useWorkspace.getState().updateScenario({ ...live, duration: 160 })
    expect(useWorkspace.getState().cursor).toBe(160)  // watching it happen: carried to the new end
    useWorkspace.getState().seek(30)
    useWorkspace.getState().set({ playing: true })
    useWorkspace.getState().updateScenario({ ...live, duration: 180 })
    expect(useWorkspace.getState().cursor).toBe(30)
    expect(useWorkspace.getState().scenarios.find(s => s.id === 'inc-1')?.duration).toBe(180)
  })
})
