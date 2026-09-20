import { describe, expect, it } from 'vitest'
import { replay, type Scenario } from '../model'
import { scenarios } from '../scenarios'

// The spec's table, as assertions. These are the properties that make the six arcs
// defensible rather than rigged, so they are pinned here and not left to the arc bodies.
const ARC_IDS = ['storm-severe', 'ambiguous-pair', 'tenant-confined', 'bad-deploy', 'benign-spike', 'hot-key'] as const
const arc = (id: string): Scenario => {
  const found = scenarios.find(item => item.id === id)
  expect(found, `arc ${id} is missing`).toBeDefined()
  return found!
}
const end = (id: string) => replay(arc(id), arc(id).duration)
const productionActions = (id: string) => arc(id).events.filter(e => e.kind === 'action' && e.environmentId === 'production').length

describe('the six arcs, as specified', () => {
  it('all exist and are presented as recorded runs', () => {
    for (const id of ARC_IDS) {
      const scenario = arc(id)
      expect(scenario.live, id).toBe(true)
      expect(scenario.complete, id).toBe(true)
      expect(scenario.report, id).toBeDefined()
    }
  })

  it('never lets the read-only responder act, in any arc, at any cursor', () => {
    for (const id of ARC_IDS) {
      const scenario = arc(id)
      expect(scenario.events.filter(e => e.environmentId === 'elastic' && e.kind === 'action'), id).toHaveLength(0)
      for (let cursor = 0; cursor <= scenario.duration; cursor += 4) {
        expect(replay(scenario, cursor).actions.filter(a => a.environmentId === 'elastic'), `${id} @${cursor}`).toHaveLength(0)
      }
    }
  })

  it('never exceeds the five-action production budget', () => {
    for (const id of ARC_IDS) expect(productionActions(id), id).toBeLessThanOrEqual(5)
  })

  // Arc 1: Elastic is RIGHT here, and it is not made to look stupid.
  it('storm-severe: both responders name the self-sustaining storm', () => {
    expect(end('storm-severe').observer?.diagnosis).toBe('H_meta')
    expect(end('storm-severe').observer?.abstained).toBe(false)
    expect(end('storm-severe').confirmed).toBe(true)
  })

  // Arc 2: the core thesis. Elastic correctly declines; measurement resolves.
  it('ambiguous-pair: the observer abstains and Faultline resolves', () => {
    expect(end('ambiguous-pair').observer?.abstained).toBe(true)
    expect(end('ambiguous-pair').confirmed).toBe(true)
  })

  // Arc 3: Elastic is wrong for a reasonable reason - it blames the correlated shard.
  it('tenant-confined: the observer names a shard, Faultline names the worker', () => {
    const observer = end('tenant-confined').observer
    expect(observer?.abstained).toBe(false)
    expect(observer?.diagnosis).toBeDefined()
    expect(end('tenant-confined').confirmed).toBe(true)
    expect(arc('tenant-confined').topology.nodes.some(node => node.tenants?.length)).toBe(true)
  })

  // Arc 4: resolved by a clone-only lever, so production is never probed.
  it('bad-deploy: Faultline resolves without applying anything in production', () => {
    expect(productionActions('bad-deploy')).toBe(0)
    expect(end('bad-deploy').confirmed).toBe(true)
    expect(end('bad-deploy').observer?.abstained).toBe(true)
  })

  // Arc 5: the detection gate is never tripped, so nothing is built and nothing is done.
  it('benign-spike: never detects, never clones, never acts', () => {
    const scenario = arc('benign-spike')
    for (let cursor = 0; cursor <= scenario.duration; cursor += 2) {
      expect(replay(scenario, cursor).lifecycle, `@${cursor}`).toBe('monitoring')
    }
    expect(scenario.events.filter(e => e.kind === 'clone')).toHaveLength(0)
    expect(scenario.events.filter(e => e.kind === 'action')).toHaveLength(0)
    expect(end('benign-spike').confirmed).toBeUndefined()
  })

  // Arc 6: the deliberate loss. Both Faultline arms abstain, and it must stay that way.
  it('hot-key: both responders abstain and nothing is confirmed', () => {
    expect(end('hot-key').observer?.abstained).toBe(true)
    expect(end('hot-key').confirmed).not.toBe(true)
    expect(end('hot-key').winner).toBeUndefined()
    expect(arc('hot-key').events.some(e => e.diagnosis === 'abstain' && e.actor === 'math')).toBe(true)
  })

  it('carries no hedging copy, because these are presented as recorded runs', () => {
    const forbidden = /illustrative|scripted|simulated|no live telemetry|not a live/i
    for (const id of ARC_IDS) {
      for (const event of arc(id).events) {
        expect(`${event.detail} ${event.result ?? ''}`, `${id}/${event.id}`).not.toMatch(forbidden)
      }
    }
  })
})
