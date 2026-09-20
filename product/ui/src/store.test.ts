import { beforeEach, describe, expect, it } from 'vitest'
import { useWorkspace } from './store'

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
  it('updateScenario keeps a viewer at the end of the replay at the new end, others where they are', async () => {
    const { useWorkspace } = await import('./store')
    const base = useWorkspace.getState().scenarios[0]
    const live = { ...base, id: 'inc-1', live: true, complete: false, duration: 100, events: base.events.filter(e => e.at <= 100) }
    useWorkspace.getState().addScenarios([live], 'inc-1')
    useWorkspace.getState().seek(100)
    useWorkspace.getState().updateScenario({ ...live, duration: 140 })
    expect(useWorkspace.getState().cursor).toBe(140)
    useWorkspace.getState().seek(30)
    useWorkspace.getState().updateScenario({ ...live, duration: 180 })
    expect(useWorkspace.getState().cursor).toBe(30)
    expect(useWorkspace.getState().scenarios.find(s => s.id === 'inc-1')?.duration).toBe(180)
  })
})
