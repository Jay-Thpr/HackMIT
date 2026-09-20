import { describe, expect, it } from 'vitest'
import { replay } from './model'
import { scenarios } from './scenarios'

const platform = scenarios.find(scenario => scenario.id === 'platform')!

describe('the partitioned, replicated example', () => {
  it('is a distributed topology rather than a flat one', () => {
    const kinds = platform.topology.nodes.reduce<Record<string, number>>((all, node) => ({ ...all, [node.kind]: (all[node.kind] ?? 0) + 1 }), {})
    expect(platform.topology.nodes).toHaveLength(19)
    expect(kinds).toEqual({ service: 8, queue: 3, datastore: 7, external: 1 })
  })

  it('gives every node a baseline and every edge two real endpoints', () => {
    const ids = new Set(platform.topology.nodes.map(node => node.id))
    for (const edge of platform.topology.edges) {
      expect(ids.has(edge.source), edge.source).toBe(true)
      expect(ids.has(edge.target), edge.target).toBe(true)
    }
    for (const node of platform.topology.nodes) expect(platform.baseline[node.id], node.id).toBeDefined()
  })

  it('describes the target with queue vocabulary, because the target is a queue', () => {
    expect(platform.topology.nodes.find(node => node.id === platform.targetId)?.kind).toBe('queue')
    expect(platform.events.map(event => event.title)).toContain('Add transient processing latency')
  })

  it('carries resource evidence through the incident and back', () => {
    const lag = (at: number) => replay(platform, at).environments.find(environment => environment.id === 'production')!.nodes['kafka-1']?.resourceMetrics
    expect(lag(0)).toEqual({ consumer_lag_messages: 14 })
    expect(lag(36)).toEqual({ consumer_lag_messages: 8420 })
    expect(lag(platform.duration)).toEqual({ consumer_lag_messages: 14 })
  })

  it('reaches a confirmed diagnosis and removes both clones', () => {
    const end = replay(platform, platform.duration)
    expect(end.confirmed).toBe(true)
    expect(end.diagnosis).toBe('A')
    expect(end.cleanup).toBe('complete')
  })
})
