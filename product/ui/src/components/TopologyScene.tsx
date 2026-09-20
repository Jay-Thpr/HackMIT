import { Component, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { CameraControls, Html, Line, RoundedBox } from '@react-three/drei'
import * as THREE from 'three'
import type { GraphLayout } from '../layout'
import { environmentOutcomeLabel, environmentPresence, type Entity, type Environment, type Position, type Scenario, type WorkspaceState } from '../model'
import { useWorkspace } from '../store'
import { deriveNodeRecovery, type NodeRecovery } from '../recovery'
import { agentActivity } from '../agent-activity'
import { suiteChecks } from '../suite'

const trailColor = '#e38a50'
// Structure health: visible but muted, so it reads as a state change rather than an alert graphic.
const degradedColor = '#d09a90'
const recoveringColor = '#e9cfa6'
const healthyColor = '#f5f3ed'


export function investigationPath(layout: GraphLayout, entry: string, target?: string): Set<string> {
  const queue: { node: string; edges: string[] }[] = [{ node: entry, edges: [] }]
  const visited = new Set<string>()
  while (queue.length) {
    const current = queue.shift()!
    if (current.node === target) return new Set(current.edges)
    if (visited.has(current.node)) continue
    visited.add(current.node)
    for (const edge of layout.edges.filter(edge => edge.source === current.node)) queue.push({ node: edge.target, edges: [...current.edges, edge.id] })
  }
  return new Set()
}

// Each environment is one suspended level; edges retain smooth three-dimensional curves.
function spatialLayout(layout: GraphLayout): GraphLayout {
  const positions: Record<string, Position> = Object.fromEntries(Object.entries(layout.positions).map(([id, position]) => {
    return [id, [position[0] * 1.08, 0, position[2] * 0.82]]
  }))
  const edges = layout.edges.map(edge => {
    const source = new THREE.Vector3(...positions[edge.source])
    const target = new THREE.Vector3(...positions[edge.target])
    const direction = target.clone().sub(source).normalize()
    const start = source.clone().addScaledVector(direction, 0.62)
    const end = target.clone().addScaledVector(direction, -0.62)
    const offset = new THREE.Vector3(0, edge.source.localeCompare(edge.target) < 0 ? 0.8 : -0.8, 0)
    const curve = new THREE.CubicBezierCurve3(start, start.clone().lerp(end, 0.32).add(offset), start.clone().lerp(end, 0.68).add(offset), end)
    return { ...edge, points: curve.getPoints(36).map(point => point.toArray() as Position) }
  })
  return { ...layout, positions, edges, width: layout.width * 1.08, height: layout.height * 0.82 }
}

function placement(environment: Environment): { position: Position; scale: number } {
  return { position: [0, environment.level * 5.2, 0], scale: 1 }
}

function useIsolationOpacity(faded: boolean) {
  const { reducedMotion, playing, follow, cursor } = useWorkspace()
  const opacity = useRef(faded ? 0.13 : 1)
  const previousCursor = useRef(cursor)
  const invalidate = useThree(state => state.invalidate)
  useFrame((_, delta) => {
    const target = faded ? 0.13 : 1
    const seek = cursor < previousCursor.current || (!playing && cursor !== previousCursor.current)
    previousCursor.current = cursor
    if (reducedMotion || seek) opacity.current = target
    else if (Math.abs(opacity.current - target) > 0.001 && (playing || !follow)) {
      opacity.current = THREE.MathUtils.damp(opacity.current, target, 5, delta)
      invalidate()
    }
  })
  return opacity
}

function useReplayColor(color: string) {
  const { cursor, playing, reducedMotion } = useWorkspace()
  const displayed = useRef(new THREE.Color(color))
  const target = useMemo(() => new THREE.Color(color), [color])
  const previousCursor = useRef(cursor)
  useFrame(() => {
    const elapsed = cursor - previousCursor.current
    previousCursor.current = cursor
    if (reducedMotion || elapsed < 0 || elapsed > 0.5 || (!playing && elapsed !== 0)) displayed.current.copy(target)
    else if (elapsed > 0) displayed.current.lerp(target, 1 - Math.exp(-elapsed * 4))
  })
  return displayed.current
}

const projected = new THREE.Vector3()

/** Keep an overlay control reachable when a close camera pushes its anchor outside the canvas. */
function edgeSafePosition(object: THREE.Object3D, camera: THREE.Camera, size: { width: number; height: number }): [number, number] {
  projected.setFromMatrixPosition(object.matrixWorld).project(camera)
  if (projected.z > 1) return [-4000, -4000]  // behind the camera: keep it out of the frame entirely
  const inset = 26
  return [
    THREE.MathUtils.clamp(((projected.x + 1) * size.width) / 2, inset, Math.max(inset, size.width - inset)),
    THREE.MathUtils.clamp(((-projected.y + 1) * size.height) / 2, inset, Math.max(inset, size.height - inset)),
  ]
}

function ServiceNode({ entity, environment, position, attention, interactive, faded, healthState, presence }: { entity: Entity; environment: Environment; position: Position; attention: boolean; interactive: boolean; faded: boolean; healthState: NodeRecovery; presence: number }) {
  const selected = useWorkspace(state => state.selectedNode === entity.id && state.environmentId === environment.id)
  const { inspect, set, reducedMotion, cursor } = useWorkspace()
  const group = useRef<THREE.Group>(null)
  const marker = useRef<THREE.Mesh>(null)
  const invalidate = useThree(state => state.invalidate)
  const color = healthState === 'degraded' ? degradedColor : healthState === 'recovering' ? recoveringColor : healthyColor
  const displayedColor = useReplayColor(color)
  const isolationOpacity = useIsolationOpacity(faded)
  const opacity = (faded ? 0.13 : 1) * presence
  const available = presence > 0.15
  const open = () => { if (!available) return; inspect(entity.id, environment.id); set({ traceTab: 'trace', follow: false }) }
  useFrame((_, delta) => {
    if (group.current) {
      group.current.traverse(object => {
        if (object instanceof THREE.Mesh && object.material instanceof THREE.MeshStandardMaterial) {
          object.material.color.copy(displayedColor)
          object.material.opacity = isolationOpacity.current * presence
        }
      })
      const scale = selected ? 1.13 : 1
      if (Math.abs(group.current.scale.x - scale) > 0.001) {
        group.current.scale.setScalar(THREE.MathUtils.damp(group.current.scale.x, scale, reducedMotion ? 10000 : 6, delta))
        invalidate()
      }
    }
    if (marker.current) {
      const angle = reducedMotion ? 0 : cursor * 1.4
      marker.current.position.set(Math.cos(angle) * 0.85, Math.sin(angle) * 0.85, 0.15)
    }
  })
  return <group position={position} ref={group}>
    <group onClick={available ? event => { event.stopPropagation(); open() } : undefined}>
      {entity.kind === 'datastore' ? <group>
        <mesh><cylinderGeometry args={[0.62, 0.62, 1.05, 48]} /><meshStandardMaterial color={displayedColor} transparent opacity={opacity} depthWrite={!faded && presence > 0.99} roughness={0.4} metalness={0.08} /></mesh>
        {[-0.24, 0.19].map(height => <mesh key={height} position={[0, height, 0]} rotation={[Math.PI / 2, 0, 0]}><torusGeometry args={[0.624, 0.012, 6, 48]} /><meshBasicMaterial color="#d6ddd0" transparent opacity={opacity} depthWrite={false} toneMapped={false} /></mesh>)}
      </group> : entity.kind === 'external' ? <mesh rotation={[0.1, Math.PI / 4, 0.1]}><icosahedronGeometry args={[0.75, 0]} /><meshStandardMaterial color={displayedColor} transparent opacity={opacity} depthWrite={!faded && presence > 0.99} roughness={0.35} metalness={0.06} /></mesh> : <group>
        <RoundedBox args={[1.3, 0.9, 1.05]} radius={0.15} smoothness={5}><meshStandardMaterial color={displayedColor} transparent opacity={opacity} depthWrite={!faded && presence > 0.99} roughness={0.38} metalness={0.04} /></RoundedBox>
        {entity.kind === 'queue' && [-0.18, 0.1].map(height => <mesh key={height} position={[0, height, 0.53]}><boxGeometry args={[0.84, 0.022, 0.01]} /><meshBasicMaterial color="#d6ddd0" transparent opacity={opacity} depthWrite={false} toneMapped={false} /></mesh>)}
      </group>}
      <mesh position={[0.32, 0.2, 0.535]}><sphereGeometry args={[0.05, 12, 12]} /><meshBasicMaterial color={selected || attention ? trailColor : '#d6ddd0'} transparent opacity={opacity} depthWrite={false} toneMapped={false} /></mesh>
    </group>
    {selected && <group>
      <mesh><torusGeometry args={[0.87, 0.01, 6, 64]} /><meshBasicMaterial color={trailColor} transparent opacity={(faded ? 0.08 : 0.5) * presence} depthWrite={false} toneMapped={false} /></mesh>
      <mesh ref={marker} position={[0.85, 0, 0.15]}><sphereGeometry args={[0.055, 12, 12]} /><meshBasicMaterial color={trailColor} transparent opacity={opacity} depthWrite={false} toneMapped={false} /></mesh>
    </group>}
    {(healthState === 'degraded' || healthState === 'recovering') && !faded && available && <Html position={[0, 1.05, 0]} center zIndexRange={[34, 0]} calculatePosition={edgeSafePosition} style={{ opacity: presence }}><button className={`node-issue-marker ${healthState === 'recovering' ? 'is-recovering' : ''}`} aria-label={`Inspect issue in ${entity.label} in ${environment.label}`} onClick={open} title={healthState === 'degraded' ? 'Measured degradation — inspect issue' : 'Recovery observed — confirmation pending'}>!</button></Html>}
    {interactive && available && <Html center zIndexRange={[30, 0]}><button className="node-hit-target" aria-label={`Inspect ${entity.label} in ${environment.label}`} aria-pressed={selected} onClick={open} style={{ width: 44, height: 44, padding: 0, background: 'transparent', border: 0, borderRadius: 12, cursor: 'pointer' }} /></Html>}
  </group>
}

function Traffic({ layout, environment, path, faded, presence }: { layout: GraphLayout; environment: Environment; path: Set<string>; faded: boolean; presence: number }) {
  const mesh = useRef<THREE.InstancedMesh>(null)
  const dummy = useMemo(() => new THREE.Object3D(), [])
  const { cursor, reducedMotion } = useWorkspace()
  const particles = useMemo(() => layout.edges.flatMap(edge => {
    if (!path.has(edge.id) || !environment.nodes[edge.source]?.qps) return []
    const curve = new THREE.CatmullRomCurve3(edge.points.map(point => new THREE.Vector3(...point)))
    return [0, 0.35, 0.7].map(offset => ({ curve, offset }))
  }), [layout, environment.nodes, path])
  const draw = () => {
    if (!mesh.current) return
    particles.forEach((particle, index) => {
      dummy.position.copy(particle.curve.getPoint((particle.offset + (reducedMotion ? 0 : cursor) * 0.13) % 1))
      dummy.scale.setScalar(0.042)
      dummy.updateMatrix()
      mesh.current!.setMatrixAt(index, dummy.matrix)
    })
    mesh.current.count = particles.length
    mesh.current.instanceMatrix.needsUpdate = true
  }
  useFrame(draw)
  return <instancedMesh ref={mesh} args={[undefined, undefined, Math.max(1, particles.length)]} frustumCulled={false}><sphereGeometry args={[1, 10, 8]} /><meshBasicMaterial color={trailColor} transparent opacity={(faded ? 0.1 : 1) * presence} depthWrite={false} toneMapped={false} /></instancedMesh>
}

function TestColumn({ check, environment, position, faded, presence }: { check: ReturnType<typeof suiteChecks>[number]; environment: Environment; position: Position; faded: boolean; presence: number }) {
  const { selectedSuiteCheck, focus, set } = useWorkspace()
  const selected = selectedSuiteCheck?.environmentId === environment.id && selectedSuiteCheck.checkId === check.id
  const material = useRef<THREE.MeshStandardMaterial>(null)
  const color = check.state === 'passed' ? '#79bd82' : '#d9564d'
  const displayedColor = useReplayColor(color)
  const isolationOpacity = useIsolationOpacity(faded)
  const opacity = (faded ? 0.13 : 1) * presence
  const available = presence > 0.15
  const open = () => { if (!available) return; focus(environment.id); set({ selectedSuiteCheck: { environmentId: environment.id, checkId: check.id }, traceTab: 'trace', follow: false }) }
  useFrame(() => {
    if (!material.current) return
    material.current.color.copy(displayedColor)
    material.current.opacity = isolationOpacity.current * presence
  })
  return <group position={position}>
    <RoundedBox args={[0.14, 0.28, 0.14]} radius={0.02} smoothness={3} onClick={available ? event => { event.stopPropagation(); open() } : undefined}>
      <meshStandardMaterial ref={material} color={displayedColor} transparent opacity={opacity} depthWrite={!faded && presence > 0.99} roughness={0.38} metalness={0.06} emissive={color} emissiveIntensity={selected ? 0.2 : 0.025} />
    </RoundedBox>
    {!faded && available && <Html center distanceFactor={8} zIndexRange={[36, 0]}><button className="node-hit-target test-column-hit-target" data-suite-check={check.id} title={`${check.label} · ${check.state}${check.total > 1 ? ` · ${check.passed}/${check.total} passed` : ''}`} aria-label={`${check.label}: ${check.state} in ${environment.label}`} aria-pressed={selected} onClick={open} style={{ width: 24, height: 30, padding: 0, background: 'transparent', border: 0, borderRadius: 4, cursor: 'pointer' }} /></Html>}
  </group>
}

function AgentMarker({ scenario, environment, layout, faded, presence }: { scenario: Scenario; environment: Environment; layout: GraphLayout; faded: boolean; presence: number }) {
  const { cursor, reducedMotion, inspect, set } = useWorkspace()
  const activity = agentActivity(scenario, environment.id, cursor)
  const target = layout.positions[activity.targetId]
  const route = useMemo(() => {
    const steps: { at: number; from: THREE.Vector3; to: THREE.Vector3; targetId: string }[] = []
    for (const event of scenario.events) {
      if (event.environmentId !== environment.id || !event.targetId || !['reason', 'action', 'observe', 'undo', 'verdict'].includes(event.kind)) continue
      const position = layout.positions[event.targetId]
      const previous = steps.at(-1)
      if (!position || previous?.targetId === event.targetId) continue
      const to = new THREE.Vector3(...position)
      const progress = previous ? THREE.MathUtils.clamp((event.at - previous.at) / 0.8, 0, 1) : 1
      const from = previous ? previous.from.clone().lerp(previous.to, 1 - (1 - progress) ** 3) : to.clone()
      steps.push({ at: event.at, from, to, targetId: event.targetId })
    }
    return steps
  }, [scenario, environment.id, layout])
  const step = route.filter(step => step.at <= cursor).at(-1)
  const progress = reducedMotion ? 1 : THREE.MathUtils.clamp((cursor - (step?.at ?? cursor)) / 0.8, 0, 1)
  const position = step ? step.from.clone().lerp(step.to, 1 - (1 - progress) ** 3) : new THREE.Vector3(...(target ?? [0, 0, 0]))
  const scanTime = activity.phase === 'Complete' || environment.lifecycle === 'destroying' ? (environment.lifecycle === 'destroying' ? environment.lifecycleAt : activity.event?.at ?? cursor) : cursor
  const scanHeight = reducedMotion ? 0 : -0.35 + (Math.sin(scanTime * 1.7) + 1) * 0.4
  const available = presence > 0.15
  const open = () => { if (!available) return; inspect(activity.targetId, environment.id); set({ selectedAgent: environment.id, follow: false }) }
  if (!activity.event || !target) return null
  return <group position={position}>
    <mesh position={[0, scanHeight, 0]} rotation={[Math.PI / 2, 0, 0]}><torusGeometry args={[0.8, 0.008, 6, 64]} /><meshBasicMaterial color="#e1ac83" transparent opacity={(faded ? 0.04 : 0.32) * presence} depthWrite={false} /></mesh>
    <group position={[0.98, 0.8, 0]}><mesh onClick={available ? event => { event.stopPropagation(); open() } : undefined}><octahedronGeometry args={[0.115]} /><meshBasicMaterial color="#edbd95" transparent opacity={(faded ? 0.15 : 1) * presence} depthWrite={false} /></mesh>
    {!faded && available && <Html center zIndexRange={[40, 0]} style={{ opacity: presence }}><button className="agent-hit-target" aria-label={`Inspect ${activity.name}`} title={`${activity.name} · ${activity.phase}`} onClick={open} /></Html>}</group>
  </group>
}

function EnvironmentCluster({ environment, layout, scenario, workspace }: { environment: Environment; layout: GraphLayout; scenario: Scenario; workspace: WorkspaceState }) {
  const { position, scale } = placement(environment)
  const { environmentId, isolatedLayer, focus, cursor, reducedMotion } = useWorkspace()
  const presence = environmentPresence(environment, cursor, reducedMotion)
  const removing = environment.lifecycle === 'destroying'
  const emphasised = environment.outcome === 'confirmed' || environment.outcome === 'fix-verified'
  const lifecycleLabel = environment.outcome ? environmentOutcomeLabel[environment.outcome] : environment.lifecycle === 'starting' ? 'Starting' : removing ? 'Removing' : environment.lifecycle === 'archived' ? 'Archived' : undefined
  const travel = reducedMotion || environment.lifecycle === 'archived' ? 0 : (1 - presence) * (removing ? 0.18 : -0.35)
  const faded = isolatedLayer !== null && isolatedLayer !== environment.id
  const activeEvent = scenario.events.filter(event => event.at <= cursor && event.targetId).at(-1)
  const activeActions = workspace.actions.filter(action => action.environmentId === environment.id && action.status !== 'reverted')
  const currentEvent = scenario.events.filter(event => event.at <= cursor && event.environmentId === environment.id && event.targetId).at(-1)
  const path = useMemo(() => investigationPath(layout, scenario.entryId, currentEvent?.targetId), [layout, scenario.entryId, currentEvent?.targetId])
  return <group position={position}>
    <Html position={[-layout.width / 2 + 0.3, 0.6, 0]} center zIndexRange={[35, 0]}><button className={`environment-label ${environmentId === environment.id ? 'is-focused' : ''}`} data-environment={environment.id} data-lifecycle={environment.lifecycle} data-outcome={environment.outcome} data-emphasised={emphasised || undefined} data-presence={presence.toFixed(3)} data-level={environment.level} aria-label={`${environment.label}${lifecycleLabel ? ` · ${lifecycleLabel}` : ''}`} onClick={() => focus(environment.id)} style={{ opacity: faded ? 0.45 : 1 }}><span>{environment.label}</span>{lifecycleLabel && <span> · {lifecycleLabel}</span>}</button></Html>
    <group position={[0, travel, 0]} scale={scale * (reducedMotion ? 1 : 0.96 + 0.04 * presence)} visible={presence > 0.001}>
      {environment.id !== 'production' && layout.positions[scenario.targetId] && <group position={layout.positions[scenario.targetId]}>
        {suiteChecks(scenario, environment.id, cursor).map((check, checkIndex) => {
          const offset = checkIndex - 1.5
          return <TestColumn key={check.id} check={check} environment={environment} faded={faded} presence={presence} position={[offset * 0.38, -0.48, 0.94 + (1 - Math.abs(offset) / 1.5) * 0.2]} />
        })}
      </group>}
      {layout.edges.map(edge => <Line key={edge.id} points={edge.points} color={path.has(edge.id) ? trailColor : '#bcc5bd'} lineWidth={path.has(edge.id) ? 1.3 : 0.75} transparent opacity={(faded ? 0.1 : path.has(edge.id) ? 0.95 : 0.65) * presence} depthWrite={false} />)}
      <AgentMarker scenario={scenario} environment={environment} layout={layout} faded={faded} presence={presence} />
      <Traffic layout={layout} environment={environment} path={path} faded={faded} presence={presence} />
      {scenario.topology.nodes.map(node => <ServiceNode key={node.id} entity={node} environment={environment} position={layout.positions[node.id]} interactive={environmentId === environment.id} faded={faded} presence={presence} healthState={deriveNodeRecovery(scenario, environment, node.id, cursor)} attention={(activeEvent?.environmentId === environment.id && activeEvent.targetId === node.id) || activeActions.some(action => action.targetId === node.id)} />)}
    </group>
  </group>
}

function CameraDirector({ layout, workspace, scenario }: { layout: GraphLayout; workspace: WorkspaceState; scenario: Scenario }) {
  const controls = useRef<CameraControls>(null)
  const { size, camera } = useThree()
  const { environmentId, isolatedLayer, selectedNode, focusRevision, follow, reducedMotion, cursor, playing, set } = useWorkspace()
  const lastEvent = useRef('')
  const automaticMotion = useRef(false)
  const resumeMotion = useRef(false)
  const manualControl = useRef(false)
  const previousCursor = useRef(cursor)
  const previousLayout = useRef(layout)
  const environmentKey = workspace.environments.map(environment => `${environment.id}:${environment.level}`).join('|')
  const move = (environment?: string, targetId?: string, isolate = false, transition = true) => {
    if (!controls.current) return
    const selectedEnvironment = workspace.environments.find(env => env.id === environment)
    if (environment && !selectedEnvironment) return
    const selected = selectedEnvironment ? placement(selectedEnvironment) : undefined
    const target = targetId ? layout.positions[targetId] : undefined
    const direction = new THREE.Vector3(0.38, 0.72, 1.65).normalize()
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), direction).normalize()
    const up = new THREE.Vector3().crossVectors(direction, right).normalize()
    const points: THREE.Vector3[] = []
    const clusters = selected && (isolate || target) ? [selected] : workspace.environments.map(environment => placement(environment))
    for (const cluster of clusters) {
      for (const node of target ? [target] : Object.values(layout.positions)) {
        const origin = new THREE.Vector3(...cluster.position).add(new THREE.Vector3(...node).multiplyScalar(cluster.scale))
        const padding = target ? 2.1 : 1.2
        for (const x of [-padding, padding]) for (const y of [-padding, padding]) for (const z of [-padding, padding]) points.push(origin.clone().add(new THREE.Vector3(x, y, z).multiplyScalar(cluster.scale)))
      }
    }
    if (!points.length) return
    const center = new THREE.Box3().setFromPoints(points).getCenter(new THREE.Vector3())
    const fov = THREE.MathUtils.degToRad((camera as THREE.PerspectiveCamera).fov || 40)
    const tanV = Math.tan(fov / 2) * Math.max(0.7, (size.height - 80) / size.height)
    const tanH = Math.tan(fov / 2) * size.width / size.height * Math.max(0.75, (size.width - 70) / size.width)
    const distance = Math.max(4, ...points.map(point => { const relative = point.clone().sub(center); return Math.max(Math.abs(relative.dot(up)) / tanV, Math.abs(relative.dot(right)) / tanH) + relative.dot(direction) }))
    const position = center.clone().add(direction.multiplyScalar(distance))
    void controls.current.setLookAt(position.x, position.y, position.z, center.x, center.y, center.z, transition && !reducedMotion)
  }
  useEffect(() => {
    if (!playing && automaticMotion.current) {
      if (controls.current) {
        const position = controls.current.getPosition(new THREE.Vector3(), false)
        const target = controls.current.getTarget(new THREE.Vector3(), false)
        void controls.current.setLookAt(position.x, position.y, position.z, target.x, target.y, target.z, false)
      }
      automaticMotion.current = false
      resumeMotion.current = true
    }
  }, [playing])
  useEffect(() => {
    if (follow) return
    if (manualControl.current) { manualControl.current = false; return }
    automaticMotion.current = false
    const environment = workspace.environments.some(environment => environment.id === environmentId) ? environmentId : 'production'
    move(environment, selectedNode, isolatedLayer !== null)
  }, [layout, focusRevision, environmentId, isolatedLayer, selectedNode, follow, reducedMotion, size.width, size.height, environmentKey])
  useEffect(() => {
    const seek = cursor < previousCursor.current || (!playing && cursor !== previousCursor.current)
    previousCursor.current = cursor
    if (!follow) { lastEvent.current = ''; resumeMotion.current = false; return }
    const event = scenario.events.filter(event => event.at <= cursor && (
      ['clone', 'lifecycle', 'archive'].includes(event.kind) ||
      (event.targetId && ['detect', 'reason', 'verdict', 'observe', 'action', 'undo'].includes(event.kind) && workspace.environments.some(environment => environment.id === event.environmentId))
    )).at(-1)
    const key = `${scenario.id}:${event?.id ?? 'baseline'}:${environmentKey}:${size.width}:${size.height}:${reducedMotion}`
    if (key === lastEvent.current && layout === previousLayout.current && !(playing && resumeMotion.current)) return
    resumeMotion.current = false
    const enablingFollow = lastEvent.current === ''
    lastEvent.current = key
    previousLayout.current = layout
    const lifecycle = !event || ['clone', 'lifecycle', 'archive'].includes(event.kind)
    const environment = lifecycle ? 'production' : event.environmentId
    const target = lifecycle ? undefined : event.targetId
    set({ environmentId: environment, isolatedLayer: lifecycle ? null : environment, selectedNode: undefined, selectedAgent: undefined, selectedSuiteCheck: undefined })
    automaticMotion.current = playing
    move(environment, target, false, !seek && (playing || enablingFollow))
  }, [cursor, follow, playing, scenario, environmentKey, layout, size.width, size.height, reducedMotion])
  return <CameraControls ref={controls} makeDefault smoothTime={0.65} draggingSmoothTime={0.22} minDistance={3} maxDistance={100} minPolarAngle={0.12} maxPolarAngle={Math.PI - 0.12} onControlStart={() => { automaticMotion.current = false; manualControl.current = follow; set({ follow: false }) }} />
}

class SceneBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}

export default function TopologyScene({ layout, workspace, scenario, fallback }: { layout: GraphLayout; workspace: WorkspaceState; scenario: Scenario; fallback: ReactNode }) {
  const { playing, reducedMotion } = useWorkspace()
  const [visible, setVisible] = useState(!document.hidden)
  const spatial = useMemo(() => spatialLayout(layout), [layout])
  useEffect(() => { const update = () => setVisible(!document.hidden); document.addEventListener('visibilitychange', update); return () => document.removeEventListener('visibilitychange', update) }, [])
  const animate = playing && !reducedMotion && visible
  return <SceneBoundary fallback={fallback}><Canvas dpr={[1, 1.5]} camera={{ position: [12, 15, 28], fov: 40, near: 0.1, far: 200 }} frameloop={visible ? animate ? 'always' : 'demand' : 'never'} fallback={fallback} gl={{ antialias: true, powerPreference: 'low-power' }}>
    <color attach="background" args={['#252a27']} />
    <ambientLight intensity={1.1} />
    <directionalLight position={[-6, 12, 9]} intensity={2.5} />
    <directionalLight position={[8, 1, -6]} intensity={0.8} color="#E0E7D7" />
    <Suspense fallback={null}>{workspace.environments.map(environment => <EnvironmentCluster key={`${scenario.id}:${environment.id}`} environment={environment} layout={spatial} scenario={scenario} workspace={workspace} />)}</Suspense>
    <CameraDirector layout={spatial} workspace={workspace} scenario={scenario} />
  </Canvas></SceneBoundary>
}
