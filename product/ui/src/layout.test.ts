import { describe, expect, it } from 'vitest'
import ELK from 'elkjs/lib/elk.bundled.js'
import { deriveTopology } from './model'
import { ASPECT_MAX, ASPECT_MIN, layoutGraph, MIN_SEPARATION } from './layout'
import { scenarios } from './scenarios'

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

// The 19-node partitioned topology is the hardest case the workspace renders: three brokers,
// three shards each with a streaming replica, a consumer group of three workers. With two
// different scale factors (0.034 across a layer against 0.018 through them) it came out
// 32.7 x 14.6 - a ribbon seen edge-on from the elevated camera.
describe('the 19-node partitioned topology', () => {
  const platform = scenarios.find(scenario => scenario.id === 'platform')!

  it('is nineteen nodes, so these assertions are about the hard case', () => {
    expect(platform.topology.nodes).toHaveLength(19)
  })

  it('occupies a roughly square footprint rather than a ribbon', async () => {
    const layout = await layoutGraph(platform.topology, engine)
    const ratio = layout.width / layout.height
    // ELK arranges these 19 nodes in five tiers of at most six, a box of 962 x 940 of its own
    // units - very nearly square. Anything outside 0.7 - 1.5 therefore means the mapping into
    // 3D has stretched one axis against the other rather than the graph genuinely being wide:
    // 1.5 is the point past which the widest tier dominates and the layers collapse into a
    // band, and 0.7 is its mirror image with the depth axis overstretched.
    expect(ratio).toBeGreaterThan(0.7)
    expect(ratio).toBeLessThan(1.5)
  })

  it('keeps every pair of nodes at least a node-width apart in the ground plane', async () => {
    const layout = await layoutGraph(platform.topology, engine)
    const ids = Object.keys(layout.positions)
    expect(ids).toHaveLength(19)
    // A node mesh is at most 1.74 across in the XZ plane (the 0.87-radius selection ring;
    // the bodies themselves are 1.24 - 1.5). TopologyScene then compresses the depth axis by
    // 0.82, so a minimum centre separation of 3.4 still leaves 3.4 * 0.82 = 2.79 between
    // depth-adjacent centres - about 1.6x the widest footprint, which keeps a clear channel
    // for the edge curves and the HTML labels. ELK's own minimum here is 100 + 64 = 164 units
    // within a tier, so this is the scale factor being asserted, not ELK's spacing.
    const minimum = 3.4
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const [ax, , az] = layout.positions[ids[i]]
        const [bx, , bz] = layout.positions[ids[j]]
        const separation = Math.hypot(ax - bx, az - bz)
        expect(separation, `${ids[i]} vs ${ids[j]}`).toBeGreaterThanOrEqual(minimum)
      }
    }
  })
})

// Every arc now has its own silhouette (5 to 19 nodes), so the bounds established for the
// 19-node case have to hold for all of them. Small graphs are the likelier failure: a handful
// of nodes in two tiers can easily land outside the aspect band.
describe('every scenario topology', () => {
  for (const scenario of scenarios) {
    it(`${scenario.id}: stays inside the aspect band and keeps nodes a node-width apart`, async () => {
      const layout = await layoutGraph(scenario.topology, engine)
      const ratio = layout.width / layout.height
      expect(ratio, `${scenario.id} aspect ${ratio.toFixed(3)}`).toBeGreaterThanOrEqual(ASPECT_MIN)
      expect(ratio, `${scenario.id} aspect ${ratio.toFixed(3)}`).toBeLessThanOrEqual(ASPECT_MAX)
      const ids = Object.keys(layout.positions)
      expect(ids).toHaveLength(scenario.topology.nodes.length)
      for (let i = 0; i < ids.length; i++) {
        for (let j = i + 1; j < ids.length; j++) {
          const [ax, , az] = layout.positions[ids[i]]
          const [bx, , bz] = layout.positions[ids[j]]
          expect(Math.hypot(ax - bx, az - bz), `${scenario.id}: ${ids[i]} vs ${ids[j]}`).toBeGreaterThanOrEqual(MIN_SEPARATION - 1e-9)
        }
      }
    })
  }
})
