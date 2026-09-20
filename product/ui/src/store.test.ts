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
