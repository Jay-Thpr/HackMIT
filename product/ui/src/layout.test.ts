import { describe, expect, it } from 'vitest'
import ELK from 'elkjs/lib/elk.bundled.js'
import { deriveTopology } from './model'
import { layoutGraph } from './layout'

const engine = new ELK()

describe('generic layout', () => {
  it('handles cycles, multiple roots, and disconnected components without losing edges', async () => {
    const graph = deriveTopology({ services: { isolated: {}, a: {}, b: {}, c: {} }, edges: [{ src: 'a', dst: 'b' }, { src: 'b', dst: 'a' }, { src: 'c', dst: 'b' }] })
    const first = await layoutGraph(graph, engine)
    const second = await layoutGraph(graph, engine)
    expect(Object.keys(first.positions).sort()).toEqual(['a', 'b', 'c', 'isolated'])
    expect(first.positions).toEqual(second.positions)
    expect(first.edges).toHaveLength(3)
    expect(Object.values(first.positions).every(p => p.every(Number.isFinite))).toBe(true)
  })

  it('handles an empty graph', async () => {
    const graph = deriveTopology({ services: {}, edges: [] })
    expect((await layoutGraph(graph, engine)).positions).toEqual({})
  })
})
