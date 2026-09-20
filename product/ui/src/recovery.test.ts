import { describe, expect, it } from 'vitest'
import { replay } from './model'
import { scenarios } from './scenarios'
import { deriveNodeRecovery } from './recovery'

describe('measured node recovery', () => {
  for (const scenario of scenarios) {
    const stateAt = (time: number, environmentId = 'production') => {
      const environment = replay(scenario, time).environments.find(item => item.id === environmentId)!
      return deriveNodeRecovery(scenario, environment, scenario.targetId, time)
    }
    it(`${scenario.id}: distinguishes baseline, degradation, provisional recovery, and confirmation`, () => {
      expect(stateAt(0)).toBe('healthy')
      expect(stateAt(47)).toBe('degraded')
      expect(stateAt(72)).toBe('recovering')
      expect(stateAt(82)).toBe('recovering')
      expect(stateAt(96)).toBe('healthy')
      expect(stateAt(47)).toBe('degraded')
    })
    it(`${scenario.id}: production confirmation never confirms an isolated clone`, () => {
      expect(stateAt(58, 'clone-a')).toBe('recovering')
      expect(stateAt(66, 'clone-a')).toBe('recovering')
      expect(stateAt(96, 'clone-a')).toBe('recovering')
      expect(stateAt(67, 'clone-b')).toBe('degraded')
      expect(stateAt(96, 'clone-b')).toBe('recovering')
    })
  }
  it('keeps omitted measurements unknown even after a verdict', () => {
    const scenario = scenarios[1]
    const environment = replay(scenario, 100).environments[0]
    expect(deriveNodeRecovery(scenario, environment, 'admin-api', 100)).toBe('unknown')
    expect(deriveNodeRecovery(scenario, environment, 'missing-node', 100)).toBe('unknown')
  })
  it('does not accept a model-authored verdict as measured confirmation', () => {
    const source = scenarios[0]
    const scenario = { ...source, events: source.events.map(event => event.kind === 'verdict' ? { ...event, actor: 'model' as const } : event) }
    const environment = replay(scenario, 100).environments[0]
    expect(deriveNodeRecovery(scenario, environment, scenario.targetId, 100)).toBe('recovering')
  })
})
