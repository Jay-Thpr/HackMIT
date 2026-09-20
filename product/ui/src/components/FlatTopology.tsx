import type { GraphLayout } from '../layout'
import { metricLabel, type Environment, type Scenario } from '../model'
import { useWorkspace } from '../store'

export function FlatTopology({ scenario, layout, environment }: { scenario: Scenario; layout?: GraphLayout; environment: Environment }) {
  const inspect = useWorkspace(state => state.inspect)
  if (!layout) return <div className="fallback-list">{scenario.topology.nodes.map(node => <button key={node.id} onClick={() => inspect(node.id, environment.id)}>{node.label}<span>{environment.nodes[node.id]?.health ?? 'unknown'}</span></button>)}</div>
  const scale = 60
  const width = (layout.width + 4) * scale
  const height = (layout.height + 3) * scale
  const x = (value: number) => (value + layout.width / 2 + 2) * scale
  const y = (value: number) => (value + layout.height / 2 + 1.5) * scale
  return <div className="flat-topology" data-testid="flat-topology">
    <svg viewBox={`0 0 ${width} ${height}`} aria-label={`${environment.label} topology`} role="img">
      <defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7" fill="#93aa9d" /></marker></defs>
      {layout.edges.map(edge => <polyline key={edge.id} points={edge.points.map(p => `${x(p[0])},${y(p[2])}`).join(' ')} fill="none" stroke="#9aafa2" strokeWidth="2" markerEnd="url(#arrow)" />)}
      {scenario.topology.nodes.map(node => {
        const pos = layout.positions[node.id]
        const reading = environment.nodes[node.id]
        return <g key={node.id} transform={`translate(${x(pos[0])}, ${y(pos[2])})`} role="button" tabIndex={0} aria-label={`Inspect ${node.label} in ${environment.label}`} onClick={() => inspect(node.id, environment.id)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); inspect(node.id, environment.id) } }}>
          <rect x="-69" y="-23" width="138" height="48" rx="9" fill={reading?.health === 'degraded' ? '#faeee3' : '#fff'} stroke={reading?.health === 'degraded' ? '#d4ab8b' : '#cfdcd2'} />
          <text y="-2" textAnchor="middle" fontSize="12" fill="#293a32" fontWeight="600">{node.label}</text>
          <text y="15" textAnchor="middle" fontSize="10" fill="#6e7971">{metricLabel(reading?.latency, 'ms')} · {reading?.health ?? 'unknown'}</text>
        </g>
      })}
    </svg>
  </div>
}
