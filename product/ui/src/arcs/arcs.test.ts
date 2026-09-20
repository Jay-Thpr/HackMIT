import { describe, expect, it } from 'vitest'
import { replay } from '../model'
import { scenarios } from '../scenarios'

const arcs = scenarios.slice(3)

const byId = (id: string) => {
  const arc = arcs.find(candidate => candidate.id === id)
  if (!arc) throw new Error(`Missing arc ${id}`)
  return arc
}

const cursors = (id: string) => {
  const arc = byId(id)
  return [...new Set([0, ...arc.events.map(event => event.at), arc.duration])].sort((a, b) => a - b)
}

describe('the six workspace investigation arcs', () => {
  it('keeps Elastic read-only at every cursor', () => {
    for (const arc of arcs) {
      expect(arc.events.some(event => event.kind === 'observer' && event.actor === 'elastic'), arc.id).toBe(true)
      expect(arc.events.filter(event => event.actor === 'elastic' && event.kind === 'action'), arc.id).toHaveLength(0)
      for (const cursor of cursors(arc.id)) {
        const state = replay(arc, cursor)
        const elasticActions = state.actions.filter(action => action.environmentId === 'elastic')
        expect(elasticActions, `${arc.id} at ${cursor}`).toHaveLength(0)
      }
    }
  })

  it('never exceeds the five-action production budget', () => {
    for (const arc of arcs) {
      for (const cursor of cursors(arc.id)) {
        const productionActions = replay(arc, cursor).actions.filter(action => action.environmentId === 'production')
        expect(productionActions.length, `${arc.id} at ${cursor}`).toBeLessThanOrEqual(5)
      }
    }
  })

  it.each(['ambiguous-pair', 'bad-deploy', 'hot-key'])('%s records an Elastic abstention', id => {
    expect(replay(byId(id), byId(id).duration).observer).toMatchObject({ diagnosis: 'abstain', abstained: true })
  })

  it('shows Elastic blame the shard while Faultline confirms the worker in tenant-confined', () => {
    const arc = byId('tenant-confined')
    const state = replay(arc, arc.duration)
    const elasticConclusion = arc.events.find(event => event.actor === 'elastic' && event.kind === 'reason')
    const faultlineVerdict = arc.events.find(event => event.actor === 'math' && event.kind === 'verdict')
    expect(elasticConclusion).toMatchObject({ diagnosis: 'H_hotkey', targetId: 'shard-1' })
    expect(elasticConclusion?.abstained).toBeUndefined()
    expect(faultlineVerdict).toMatchObject({ diagnosis: 'H_cpu', targetId: 'worker-2', confirmed: true })
    expect(state).toMatchObject({ diagnosis: 'H_cpu', confirmed: true })
  })

  it('keeps bad-deploy entirely out of production actions', () => {
    expect(byId('bad-deploy').events.filter(event => event.kind === 'action' && event.environmentId === 'production')).toHaveLength(0)
  })

  it('does not detect or clone the benign spike', () => {
    const arc = byId('benign-spike')
    expect(arc.events.filter(event => event.kind === 'clone')).toHaveLength(0)
    for (const cursor of cursors(arc.id)) {
      const state = replay(arc, cursor)
      expect(state.lifecycle, `at ${cursor}`).not.toBe('detected')
      expect(state.environments.map(environment => environment.id)).not.toContain('clone-a')
      expect(state.environments.map(environment => environment.id)).not.toContain('clone-b')
    }
  })

  it('ends hot-key without a confirmed verdict and pages a human', () => {
    const arc = byId('hot-key')
    const state = replay(arc, arc.duration)
    expect(state.confirmed).toBe(false)
    expect(state.diagnosis).toBe('abstain')
    expect(arc.events.some(event => event.tool === 'orchestrator.page_human')).toBe(true)
    expect(arc.events.filter(event => event.kind === 'verdict' && event.confirmed)).toHaveLength(0)
  })

  it('has complete baseline coverage and valid topology endpoints', () => {
    for (const arc of arcs) {
      for (const node of arc.topology.nodes) expect(arc.baseline[node.id], `${arc.id}:${node.id}`).toBeDefined()
      const ids = new Set(arc.topology.nodes.map(node => node.id))
      for (const edge of arc.topology.edges) {
        expect(ids.has(edge.source), `${arc.id}:${edge.id}:source`).toBe(true)
        expect(ids.has(edge.target), `${arc.id}:${edge.id}:target`).toBe(true)
      }
    }
  })
})
