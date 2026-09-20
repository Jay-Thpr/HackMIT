import type { ElkNode } from 'elkjs/lib/elk-api'
import type { Position, Topology } from './model'

export interface GraphLayout {
  positions: Record<string, Position>
  edges: { id: string; source: string; target: string; points: Position[] }[]
  width: number
  height: number
}

export async function layoutGraph(topology: Topology, engine: { layout(graph: ElkNode): Promise<ElkNode> }): Promise<GraphLayout> {
  if (!topology.nodes.length) return { positions: {}, edges: [], width: 1, height: 1 }
  const graph = await engine.layout({
    id: 'root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'DOWN',
      'elk.spacing.nodeNode': '64',
      // Tiers are read as bands from an elevated camera, which foreshortens the layer axis,
      // so layers sit further apart than siblings within a layer.
      'elk.layered.spacing.nodeNodeBetweenLayers': '144',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.randomSeed': '7',
    },
    children: topology.nodes.map(node => ({ id: node.id, width: 100, height: 68 })),
    edges: topology.edges.map(edge => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  })
  // One isotropic scale for both axes. Two different factors (0.034 across a layer against
  // 0.018 through the layers) stretched the widest layer 1.9x and flattened a 19-node graph
  // into a ribbon; ELK already lays the graph out in sane proportions, so preserve them.
  const scale = 0.022
  const width = (graph.width ?? 1) * scale
  const height = (graph.height ?? 1) * scale
  const positions: Record<string, Position> = Object.fromEntries((graph.children ?? []).map(node => [node.id, [
    ((node.x ?? 0) + 50) * scale - width / 2,
    0,
    ((node.y ?? 0) + 34) * scale - height / 2,
  ]]))
  const edges = topology.edges.map(edge => {
    const section = graph.edges?.find(item => item.id === edge.id)?.sections?.[0]
    const points: Position[] = section ? [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map(point => [point.x * scale - width / 2, 0.15, point.y * scale - height / 2]) : [positions[edge.source], positions[edge.target]]
    return { ...edge, points }
  })
  return { positions, edges, width, height }
}
