import { Component, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { CameraControls, Html, Line, RoundedBox } from '@react-three/drei'
import * as THREE from 'three'
import type { GraphLayout } from '../layout'
import type { Entity, Environment, Position, Scenario, WorkspaceState } from '../model'
import { useWorkspace } from '../store'
import { deriveNodeRecovery, type NodeRecovery } from '../recovery'
import { agentActivity } from '../agent-activity'
import { suiteChecks } from '../suite'

const trailColor = '#e38a50'
const degradedColor = '#dfc0b6'


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

function placement(index: number, _layout: GraphLayout): { position: Position; scale: number } {
  return { position: [0, index * 5.2, 0], scale: 1 }
}

function ServiceNode({ entity, environment, position, attention, interactive, faded, healthState }: { entity: Entity; environment: Environment; position: Position; attention: boolean; interactive: boolean; faded: boolean; healthState: NodeRecovery }) {
  const selected = useWorkspace(state => state.selectedNode === entity.id && state.environmentId === environment.id)
  const { inspect, set, reducedMotion, cursor } = useWorkspace()
  const group = useRef<THREE.Group>(null)
  const marker = useRef<THREE.Mesh>(null)
  const invalidate = useThree(state => state.invalidate)
  const color = healthState === 'degraded' ? degradedColor : healthState === 'recovering' ? '#eee0d2' : '#f5f3ed'
  const displayedColor = useRef(new THREE.Color(color))
  const targetColor = useMemo(() => new THREE.Color(color), [color])
  const opacity = faded ? 0.13 : 1
  const displayedOpacity = useRef(opacity)
  const open = () => { inspect(entity.id, environment.id); set({ traceTab: 'trace', follow: false }) }
  useFrame((_, delta) => {
    if (group.current) {
      const colorDifference = Math.abs(displayedColor.current.r - targetColor.r) + Math.abs(displayedColor.current.g - targetColor.g) + Math.abs(displayedColor.current.b - targetColor.b)
      if (colorDifference > 0.001) {
        displayedColor.current.lerp(targetColor, reducedMotion ? 1 : 1 - Math.exp(-delta * 1.8))
        group.current.traverse(object => { if (object instanceof THREE.Mesh && object.material instanceof THREE.MeshStandardMaterial) object.material.color.copy(displayedColor.current) })
        invalidate()
      }
      if (Math.abs(displayedOpacity.current - opacity) > 0.001) {
        displayedOpacity.current = reducedMotion ? opacity : THREE.MathUtils.damp(displayedOpacity.current, opacity, 5, delta)
        group.current.traverse(object => {
          if (object instanceof THREE.Mesh && object.material instanceof THREE.MeshStandardMaterial) object.material.opacity = displayedOpacity.current
        })
        invalidate()
      }
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
    <group onClick={event => { event.stopPropagation(); open() }}>
      {entity.kind === 'datastore' ? <group>
        <mesh><cylinderGeometry args={[0.62, 0.62, 1.05, 48]} /><meshStandardMaterial color={displayedColor.current} transparent opacity={opacity} depthWrite={!faded} roughness={0.4} metalness={0.08} /></mesh>
        {[-0.24, 0.19].map(height => <mesh key={height} position={[0, height, 0]} rotation={[Math.PI / 2, 0, 0]}><torusGeometry args={[0.624, 0.012, 6, 48]} /><meshBasicMaterial color="#d6ddd0" transparent opacity={opacity} toneMapped={false} /></mesh>)}
      </group> : entity.kind === 'external' ? <mesh rotation={[0.1, Math.PI / 4, 0.1]}><icosahedronGeometry args={[0.75, 0]} /><meshStandardMaterial color={displayedColor.current} transparent opacity={opacity} depthWrite={!faded} roughness={0.35} metalness={0.06} /></mesh> : <group>
        <RoundedBox args={[1.3, 0.9, 1.05]} radius={0.15} smoothness={5}><meshStandardMaterial color={displayedColor.current} transparent opacity={opacity} depthWrite={!faded} roughness={0.38} metalness={0.04} /></RoundedBox>
        {entity.kind === 'queue' && [-0.18, 0.1].map(height => <mesh key={height} position={[0, height, 0.53]}><boxGeometry args={[0.84, 0.022, 0.01]} /><meshBasicMaterial color="#d6ddd0" transparent opacity={opacity} toneMapped={false} /></mesh>)}
      </group>}
      <mesh position={[0.32, 0.2, 0.535]}><sphereGeometry args={[0.05, 12, 12]} /><meshBasicMaterial color={selected || attention ? trailColor : '#d6ddd0'} transparent opacity={opacity} toneMapped={false} /></mesh>
    </group>
    {selected && <group>
      <mesh><torusGeometry args={[0.87, 0.01, 6, 64]} /><meshBasicMaterial color={trailColor} transparent opacity={faded ? 0.08 : 0.5} toneMapped={false} /></mesh>
      <mesh ref={marker} position={[0.85, 0, 0.15]}><sphereGeometry args={[0.055, 12, 12]} /><meshBasicMaterial color={trailColor} transparent opacity={opacity} toneMapped={false} /></mesh>
    </group>}
    {(healthState === 'degraded' || healthState === 'recovering') && !faded && <Html position={[0, 1.05, 0]} center zIndexRange={[34, 0]}><button className={`node-issue-marker ${healthState === 'recovering' ? 'is-recovering' : ''}`} aria-label={`Inspect issue in ${entity.label} in ${environment.label}`} onClick={open} title={healthState === 'degraded' ? 'Measured degradation — inspect issue' : 'Recovery observed — confirmation pending'}>!</button></Html>}
    {interactive && <Html center zIndexRange={[30, 0]}><button className="node-hit-target" aria-label={`Inspect ${entity.label} in ${environment.label}`} aria-pressed={selected} onClick={open} style={{ width: 44, height: 44, padding: 0, background: 'transparent', border: 0, borderRadius: 12, cursor: 'pointer' }} /></Html>}
  </group>
}

function Traffic({ layout, environment, path, animate, faded }: { layout: GraphLayout; environment: Environment; path: Set<string>; animate: boolean; faded: boolean }) {
  const mesh = useRef<THREE.InstancedMesh>(null)
  const dummy = useMemo(() => new THREE.Object3D(), [])
  const cursor = useWorkspace(state => state.cursor)
  const clock = useRef(cursor)
  useEffect(() => { clock.current = cursor }, [cursor])
  const particles = useMemo(() => layout.edges.flatMap(edge => {
    if (!path.has(edge.id) || !environment.nodes[edge.source]?.qps) return []
    const curve = new THREE.CatmullRomCurve3(edge.points.map(point => new THREE.Vector3(...point)))
    return [0, 0.35, 0.7].map(offset => ({ curve, offset }))
  }), [layout, environment.nodes, path])
  const draw = () => {
    if (!mesh.current) return
    particles.forEach((particle, index) => {
      dummy.position.copy(particle.curve.getPoint((particle.offset + clock.current * 0.13) % 1))
      dummy.scale.setScalar(0.042)
      dummy.updateMatrix()
      mesh.current!.setMatrixAt(index, dummy.matrix)
    })
    mesh.current.count = particles.length
    mesh.current.instanceMatrix.needsUpdate = true
  }
  useEffect(draw, [particles])
  useFrame((_, delta) => { if (animate) { clock.current += delta; draw() } })
  return <instancedMesh ref={mesh} args={[undefined, undefined, Math.max(1, particles.length)]} frustumCulled={false}><sphereGeometry args={[1, 10, 8]} /><meshBasicMaterial color={trailColor} transparent opacity={faded ? 0.1 : 1} toneMapped={false} /></instancedMesh>
}

function TestColumn({ check, environment, position, faded }: { check: ReturnType<typeof suiteChecks>[number]; environment: Environment; position: Position; faded: boolean }) {
  const { selectedSuiteCheck, focus, set, reducedMotion } = useWorkspace()
  const selected = selectedSuiteCheck?.environmentId === environment.id && selectedSuiteCheck.checkId === check.id
  const material = useRef<THREE.MeshStandardMaterial>(null)
  const invalidate = useThree(state => state.invalidate)
  const color = check.state === 'passed' ? '#79bd82' : '#d9564d'
  const displayedColor = useRef(new THREE.Color(color))
  const targetColor = useMemo(() => new THREE.Color(color), [color])
  const opacity = faded ? 0.13 : 1
  const displayedOpacity = useRef(opacity)
  const open = () => { focus(environment.id); set({ selectedSuiteCheck: { environmentId: environment.id, checkId: check.id }, traceTab: 'trace', follow: false }) }
  useFrame((_, delta) => {
    if (!material.current) return
    const difference = Math.abs(displayedColor.current.r - targetColor.r) + Math.abs(displayedColor.current.g - targetColor.g) + Math.abs(displayedColor.current.b - targetColor.b)
    if (difference > 0.001 || Math.abs(displayedOpacity.current - opacity) > 0.001) {
      displayedColor.current.lerp(targetColor, reducedMotion ? 1 : 1 - Math.exp(-delta * 4))
      displayedOpacity.current = reducedMotion ? opacity : THREE.MathUtils.damp(displayedOpacity.current, opacity, 5, delta)
      material.current.color.copy(displayedColor.current)
      material.current.opacity = displayedOpacity.current
      invalidate()
    }
  })
  return <group position={position}>
    <RoundedBox args={[0.14, 0.28, 0.14]} radius={0.02} smoothness={3} onClick={event => { event.stopPropagation(); open() }}>
      <meshStandardMaterial ref={material} color={displayedColor.current} transparent opacity={displayedOpacity.current} depthWrite={!faded} roughness={0.38} metalness={0.06} emissive={color} emissiveIntensity={selected ? 0.2 : 0.025} />
    </RoundedBox>
    {!faded && <Html center distanceFactor={8} zIndexRange={[36, 0]}><button className="node-hit-target test-column-hit-target" data-suite-check={check.id} title={`${check.label} · ${check.state}${check.total > 1 ? ` · ${check.passed}/${check.total} passed` : ''}`} aria-label={`${check.label}: ${check.state} in ${environment.label}`} aria-pressed={selected} onClick={open} style={{ width: 24, height: 30, padding: 0, background: 'transparent', border: 0, borderRadius: 4, cursor: 'pointer' }} /></Html>}
  </group>
}

function AgentMarker({ scenario, environment, layout, faded }: { scenario: Scenario; environment: Environment; layout: GraphLayout; faded: boolean }) {
  const { cursor, playing, reducedMotion, inspect, set } = useWorkspace()
  const activity = agentActivity(scenario, environment.id, cursor)
  const target = layout.positions[activity.targetId]
  const group = useRef<THREE.Group>(null)
  const sweep = useRef<THREE.Mesh>(null)
  const invalidate = useThree(state => state.invalidate)
  const phase = useRef(0)
  const destination = useMemo(() => new THREE.Vector3(target?.[0] ?? 0, 0, target?.[2] ?? 0), [target])
  const initial = useRef(destination.clone())
  useFrame((_, delta) => {
    if (!group.current) return
    const moving = group.current.position.distanceTo(destination) > .002
    if (moving) { group.current.position.lerp(destination, reducedMotion ? 1 : 1 - Math.exp(-delta * 5)); invalidate() }
    if (playing && !reducedMotion && activity.phase !== 'Complete') {
      phase.current += delta
      if (sweep.current) { sweep.current.position.y = -.35 + (Math.sin(phase.current * 1.7) + 1) * .4; sweep.current.scale.setScalar(1 + Math.sin(phase.current * 1.7) * .025) }
      invalidate()
    }
  })
  const open = () => { inspect(activity.targetId, environment.id); set({ selectedAgent: environment.id }) }
  if (!activity.event || !target) return null
  return <group ref={group} position={initial.current}>
    <mesh ref={sweep} rotation={[Math.PI / 2,0,0]}><torusGeometry args={[.8,.008,6,64]} /><meshBasicMaterial color="#e1ac83" transparent opacity={faded ? .04 : .32} depthWrite={false} /></mesh>
    <group position={[.98,.8,0]}><mesh onClick={event => { event.stopPropagation(); open() }}><octahedronGeometry args={[.115]} /><meshBasicMaterial color="#edbd95" transparent opacity={faded ? .15 : 1} /></mesh>
    {!faded && <Html center zIndexRange={[40,0]}><button className="agent-hit-target" aria-label={`Inspect ${activity.name}`} title={`${activity.name} · ${activity.phase}`} onClick={open} /></Html>}</group>
  </group>
}

function EnvironmentCluster({ environment, index, layout, scenario, workspace, animate }: { environment: Environment; index: number; layout: GraphLayout; scenario: Scenario; workspace: WorkspaceState; animate: boolean }) {
  const { position, scale } = placement(index, layout)
  const { environmentId, isolatedLayer, focus, cursor, reducedMotion } = useWorkspace()
  const cluster = useRef<THREE.Group>(null)
  const invalidate = useThree(state => state.invalidate)
  const entrance = useRef(index > 0 && !reducedMotion ? 0 : 1)
  useFrame((_, delta) => {
    if (!cluster.current || entrance.current >= 0.999) return
    entrance.current = reducedMotion ? 1 : THREE.MathUtils.damp(entrance.current, 1, 4, delta)
    cluster.current.scale.setScalar(scale * (0.94 + 0.06 * entrance.current))
    cluster.current.position.y = position[1] - 0.35 * (1 - entrance.current)
    invalidate()
  })
  const faded = isolatedLayer !== null && isolatedLayer !== environment.id
  const activeEvent = scenario.events.filter(event => event.at <= cursor && event.targetId).at(-1)
  const activeActions = workspace.actions.filter(action => action.environmentId === environment.id && action.status !== 'reverted')
  const currentEvent = scenario.events.filter(event => event.at <= cursor && event.environmentId === environment.id && event.targetId).at(-1)
  const path = useMemo(() => investigationPath(layout, scenario.entryId, currentEvent?.targetId), [layout, scenario.entryId, currentEvent?.targetId])
  return <group ref={cluster} position={position} scale={scale * (0.94 + 0.06 * entrance.current)}>
    <Html position={[-layout.width / 2 + 0.3, 0.6, 0]} center zIndexRange={[35, 0]}><button className={`environment-label ${environmentId === environment.id ? 'is-focused' : ''}`} onClick={() => focus(environment.id)} style={{ opacity: faded ? 0.45 : 1 }}><span>{environment.label}</span></button></Html>
    {index > 0 && layout.positions[scenario.targetId] && <group position={layout.positions[scenario.targetId]}>
      {suiteChecks(scenario, environment.id, cursor).map((check, checkIndex) => {
        const offset = checkIndex - 1.5
        return <TestColumn key={check.id} check={check} environment={environment} faded={faded} position={[offset * 0.38, -0.48, 0.94 + (1 - Math.abs(offset) / 1.5) * 0.2]} />
      })}
    </group>}
    {layout.edges.map(edge => <Line key={edge.id} points={edge.points} color={path.has(edge.id) ? trailColor : '#bcc5bd'} lineWidth={path.has(edge.id) ? 1.3 : 0.75} transparent opacity={faded ? 0.1 : path.has(edge.id) ? 0.95 : 0.65} />)}
    <AgentMarker scenario={scenario} environment={environment} layout={layout} faded={faded} />
    <Traffic layout={layout} environment={environment} path={path} animate={animate} faded={faded} />
    {scenario.topology.nodes.map(node => <ServiceNode key={node.id} entity={node} environment={environment} position={layout.positions[node.id]} interactive={environmentId === environment.id} faded={faded} healthState={deriveNodeRecovery(scenario, environment, node.id, cursor)} attention={(activeEvent?.environmentId === environment.id && activeEvent.targetId === node.id) || activeActions.some(action => action.targetId === node.id)} />)}
  </group>
}

function CameraDirector({ layout, workspace, scenario }: { layout: GraphLayout; workspace: WorkspaceState; scenario: Scenario }) {
  const controls = useRef<CameraControls>(null)
  const { size, camera } = useThree()
  const { environmentId, isolatedLayer, selectedNode, focusRevision, follow, reducedMotion, cursor, set } = useWorkspace()
  const lastEvent = useRef('')
  const move = (environment?: string, targetId?: string) => {
    if (!controls.current) return
    const index = workspace.environments.findIndex(env => env.id === environment)
    if (environment && index < 0) return
    const selected = index >= 0 ? placement(index, layout) : undefined
    const target = targetId ? layout.positions[targetId] : undefined
    const direction = new THREE.Vector3(0.38, 0.72, 1.65).normalize()
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), direction).normalize()
    const up = new THREE.Vector3().crossVectors(direction, right).normalize()
    const points: THREE.Vector3[] = []
    const clusters = selected && (isolatedLayer !== null || target) ? [selected] : workspace.environments.map((_, i) => placement(i, layout))
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
    void controls.current.setLookAt(position.x, position.y, position.z, center.x, center.y, center.z, !reducedMotion)
  }
  useEffect(() => {
    const event = follow ? scenario.events.filter(event => event.at <= cursor && event.targetId && ['verdict', 'observe', 'action'].includes(event.kind)).at(-1) : undefined
    move(event?.environmentId ?? environmentId, event?.targetId ?? selectedNode)
  }, [layout, focusRevision, isolatedLayer, selectedNode, follow, size.width, size.height, workspace.environments.length])
  useEffect(() => {
    if (!follow) return
    const event = scenario.events.filter(event => event.at <= cursor && event.targetId && ['verdict', 'observe', 'action'].includes(event.kind)).at(-1)
    if (!event) lastEvent.current = ''
    if (event && `${scenario.id}:${event.id}` !== lastEvent.current) {
      lastEvent.current = `${scenario.id}:${event.id}`
      if (workspace.environments.some(environment => environment.id === event.environmentId)) { set({ environmentId: event.environmentId, isolatedLayer: event.environmentId, selectedNode: undefined, selectedAgent: undefined, selectedSuiteCheck: undefined }); move(event.environmentId, event.targetId) }
    }
  }, [cursor, follow, scenario.id])
  return <CameraControls ref={controls} makeDefault smoothTime={0.65} draggingSmoothTime={0.22} minDistance={3} maxDistance={100} minPolarAngle={0.12} maxPolarAngle={Math.PI - 0.12} onControlStart={() => set({ follow: false })} />
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
    <Suspense fallback={null}>{workspace.environments.map((environment, index) => <EnvironmentCluster key={environment.id} environment={environment} index={index} layout={spatial} scenario={scenario} workspace={workspace} animate={animate} />)}</Suspense>
    <CameraDirector layout={spatial} workspace={workspace} scenario={scenario} />
  </Canvas></SceneBoundary>
}
