import type { ElkNode } from 'elkjs/lib/elk-api'
import type { Position, Topology } from './model'

export interface GraphLayout {
  positions: Record<string, Position>
  edges: { id: string; source: string; target: string; points: Position[] }[]
  width: number
  height: number
}

/** The footprint band the scene reads well at, exported so the tests assert the same numbers
 *  the implementation targets. Past 1.5 the widest tier dominates and the layers collapse into
 *  a band; below 0.7 the depth axis is overstretched and the graph recedes into a line. */
export const ASPECT_MIN = 0.7
export const ASPECT_MAX = 1.5
/** Minimum centre-to-centre distance between two nodes in the ground plane. A node mesh is at
 *  most 1.74 across and the scene compresses depth by 0.82, so 3.4 leaves 2.79 between
 *  depth-adjacent centres - about 1.6x the widest footprint, keeping a channel for the edge
 *  curves and the HTML labels. */
export const MIN_SEPARATION = 3.4

export async function layoutGraph(topology: Topology, engine: { layout(graph: ElkNode): Promise<ElkNode> }): Promise<GraphLayout> {
  if (!topology.nodes.length) return { positions: {}, edges: [], width: 1, height: 1 }
  const graph = await engine.layout({
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'DOWN',
      // Isotropic, and wide enough that the smallest graph still clears a node width after
      // scaling: the 3.4-unit minimum separation the scene needs is 3.4 / 0.022 = 155 units.
      'elk.spacing.nodeNode': '156',
      'elk.layered.spacing.nodeNodeBetweenLayers': '156',
      // Disconnected components (an unattached admin surface, say) are spaced like siblings.
      'elk.spacing.componentComponent': '156',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.randomSeed': '7',
    },
    children: topology.nodes.map(node => ({ id: node.id, width: 100, height: 68 })),
    edges: topology.edges.map(edge => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  })
  // One base scale for both axes. Two different fixed factors (0.034 across a layer against
  // 0.018 through the layers) stretched the widest layer 1.9x and flattened a 19-node graph
  // into a ribbon. The correction below is computed from this graph rather than fixed, which
  // is the opposite of that bug: a 5-layer chain and a 19-node mesh need different handling,
  // and only the measured ratio can say which is which.
  const scale = 0.022
  const raw = { width: (graph.width ?? 1) * scale, height: (graph.height ?? 1) * scale }
  // Keep the footprint inside the band the scene reads well at. A graph that is much deeper
  // than it is wide renders as a thin line running away from an elevated camera, so pull the
  // long axis in; never push either axis out, which would separate nodes the layout placed.
  const ratio = raw.width / raw.height
  const correction: { x: number; z: number } = ratio < ASPECT_MIN ? { x: 1, z: ratio / ASPECT_MIN }
    : ratio > ASPECT_MAX ? { x: ASPECT_MAX / ratio, z: 1 }
    : { x: 1, z: 1 }
  // The aspect correction compresses one axis, which can pull two nodes closer than the scene
  // can draw them. Restore the margin by scaling BOTH axes uniformly, which leaves the aspect
  // ratio untouched and only makes the graph larger; the camera fits whatever it is given.
  const placed = (graph.children ?? []).map(node => [
    ((node.x ?? 0) + 50) * scale * correction.x,
    ((node.y ?? 0) + 34) * scale * correction.z,
  ] as const)
  let closest = Infinity
  for (let i = 0; i < placed.length; i++) {
    for (let j = i + 1; j < placed.length; j++) {
      closest = Math.min(closest, Math.hypot(placed[i][0] - placed[j][0], placed[i][1] - placed[j][1]))
    }
  }
  const spread = Number.isFinite(closest) && closest > 0 ? Math.max(1, MIN_SEPARATION / closest) : 1
  correction.x *= spread
  correction.z *= spread
  const width = raw.width * correction.x
  const height = raw.height * correction.z
  const positions: Record<string, Position> = Object.fromEntries((graph.children ?? []).map(node => [node.id, [
    ((node.x ?? 0) + 50) * scale * correction.x - width / 2,
    0,
    ((node.y ?? 0) + 34) * scale * correction.z - height / 2,
  ]]))
  const edges = topology.edges.map(edge => {
    const section = graph.edges?.find(item => item.id === edge.id)?.sections?.[0]
    const points: Position[] = section ? [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map(point => [point.x * scale * correction.x - width / 2, 0.15, point.y * scale * correction.z - height / 2]) : [positions[edge.source], positions[edge.target]]
    return { ...edge, points }
  })
  return { positions, edges, width, height }
}
