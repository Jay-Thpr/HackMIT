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
      'elk.layered.spacing.nodeNodeBetweenLayers': '112',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.randomSeed': '7',
    },
    children: topology.nodes.map(node => ({ id: node.id, width: 100, height: 68 })),
    edges: topology.edges.map(edge => ({ id: edge.id, sources: [edge.source], targets: [edge.target] })),
  })
  const scale = 0.018
  const xScale = 0.034
  const width = (graph.width ?? 1) * xScale
  const height = (graph.height ?? 1) * scale
  const positions: Record<string, Position> = Object.fromEntries((graph.children ?? []).map(node => [node.id, [
    ((node.x ?? 0) + 50) * xScale - width / 2,
    0,
    ((node.y ?? 0) + 34) * scale - height / 2,
  ]]))
  const edges = topology.edges.map(edge => {
    const section = graph.edges?.find(item => item.id === edge.id)?.sections?.[0]
    const points: Position[] = section ? [section.startPoint, ...(section.bendPoints ?? []), section.endPoint].map(point => [point.x * xScale - width / 2, 0.15, point.y * scale - height / 2]) : [positions[edge.source], positions[edge.target]]
    return { ...edge, points }
  })
  return { positions, edges, width, height }
}
