import { Component, Suspense, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { CameraControls, Grid, Html, Line, RoundedBox } from '@react-three/drei'
import { Box, Database, ExternalLink, Layers3 } from 'lucide-react'
import * as THREE from 'three'
import type { GraphLayout } from '../layout'
import { metricLabel, type ActiveAction, type Entity, type Environment, type Position, type Scenario, type WorkspaceState } from '../model'
import { useWorkspace } from '../store'

const iconFor = { service: Box, datastore: Database, queue: Layers3, external: ExternalLink }
const healthColor = { healthy: '#76a79a', degraded: '#d58b65', unknown: '#a7acb3' }

function placement(index: number, layout: GraphLayout): { position: Position; scale: number } {
  if (index === 0) return { position: [0, 0, 2], scale: 1 }
  return { position: [(index % 2 === 1 ? -1 : 1) * (layout.width * 0.35 + 1.4), 0.4, -layout.height * 0.32 - 1.5], scale: 0.32 }
}

function ServiceNode({ entity, environment, position, actions, showLabel, attention }: {
  entity: Entity; environment: Environment; position: Position; actions: ActiveAction[]; showLabel: boolean; attention: boolean
}) {
  const selected = useWorkspace(state => state.selectedNode === entity.id && state.environmentId === environment.id)
  const inspect = useWorkspace(state => state.inspect)
  const reducedMotion = useWorkspace(state => state.reducedMotion)
  const playing = useWorkspace(state => state.playing)
  const cursor = useWorkspace(state => state.cursor)
  const invalidate = useThree(state => state.invalidate)
  const scaleTarget = useMemo(() => new THREE.Vector3(), [])
  const reading = environment.nodes[entity.id] ?? { health: 'unknown' }
  const group = useRef<THREE.Group>(null)
  const marker = useRef<THREE.Mesh>(null)
  const color = healthColor[reading.health]
  const active = actions.find(action => action.targetId === entity.id && action.status !== 'reverted')
  const Icon = iconFor[entity.kind]
  useFrame((state, delta) => {
    if (!group.current) return
    const scale = selected ? 1.08 : 1
    scaleTarget.setScalar(scale)
    if (group.current.scale.distanceToSquared(scaleTarget) > 0.000001) {
      group.current.scale.lerp(scaleTarget, reducedMotion ? 1 : 1 - Math.exp(-delta * 9))
      invalidate()
    }
    if (marker.current && playing && !reducedMotion) {
      marker.current.position.set(Math.cos(state.clock.elapsedTime * 1.8) * 0.94, 0.34, Math.sin(state.clock.elapsedTime * 1.8) * 0.94)
    }
  })
  return <group position={position} ref={group}>
    <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.045, 0]}>
      <ringGeometry args={[0.77, selected ? 0.83 : 0.79, 48]} />
      <meshBasicMaterial color={selected ? '#274e42' : color} transparent opacity={selected ? 0.95 : 0.35} />
    </mesh>
    <group position={[0, 0.34, 0]} onClick={event => { event.stopPropagation(); inspect(entity.id, environment.id) }}>
      {entity.kind === 'datastore' ? <mesh castShadow receiveShadow>
        <cylinderGeometry args={[0.55, 0.55, 0.48, 32]} />
        <meshStandardMaterial color={reading.health === 'degraded' ? '#f4d9c7' : '#e0e9e4'} roughness={0.65} />
      </mesh> : <RoundedBox args={[1.25, 0.46, 1]} radius={0.12} smoothness={4} castShadow receiveShadow rotation={entity.kind === 'external' ? [0, Math.PI / 4, 0] : [0, 0, 0]}>
        <meshStandardMaterial color={reading.health === 'degraded' ? '#f2d9c8' : '#e5ede8'} roughness={0.68} />
      </RoundedBox>}
      <mesh position={[0, 0.247, 0]} rotation={[-Math.PI / 2, 0, 0]}>
        <circleGeometry args={[0.13, 24]} /><meshBasicMaterial color={color} />
      </mesh>
    </group>
    {(active || attention) && <group>
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.075, 0]}>
        <ringGeometry args={[0.94, 0.98, 64]} /><meshBasicMaterial color={active ? '#bd8850' : environment.color} transparent opacity={0.7} />
      </mesh>
      <mesh ref={marker} position={[0.94, 0.34, 0]}><sphereGeometry args={[0.075, 12, 12]} /><meshBasicMaterial color={environment.color} /></mesh>
    </group>}
    {showLabel && <Html position={[0, 1.2, 0]} center zIndexRange={[30, 0]}>
      <button className={`node-label ${selected ? 'is-selected' : ''}`} aria-label={`Inspect ${entity.label} in ${environment.label}`} onClick={() => inspect(entity.id, environment.id)}>
        <span className="node-label-title"><Icon size={12} /><span>{entity.label}</span><i className={`health-dot ${reading.health}`} /></span>
        <span className="node-label-metric">{metricLabel(reading.latency, 'ms')}<span>{reading.health}</span></span>
        {active && <span className="node-action-label">{active.label} · {active.status === 'awaiting-reversion' ? 'Awaiting undo' : `${Math.max(0, Math.ceil(active.start + active.ttl - cursor))}s TTL`}</span>}
      </button>
    </Html>}
  </group>
}

function Traffic({ layout, environment, animate }: { layout: GraphLayout; environment: Environment; animate: boolean }) {
  const mesh = useRef<THREE.InstancedMesh>(null)
  const dummy = useMemo(() => new THREE.Object3D(), [])
  const clock = useRef(0)
  const particles = useMemo(() => layout.edges.flatMap(edge => {
    const reading = environment.nodes[edge.source]
    if (reading?.qps === undefined || reading.qps <= 0) return []
    const curve = new THREE.CurvePath<THREE.Vector3>()
    for (let i = 1; i < edge.points.length; i++) curve.add(new THREE.LineCurve3(new THREE.Vector3(...edge.points[i - 1]), new THREE.Vector3(...edge.points[i])))
    const count = Math.min(14, Math.max(3, Math.round(Math.sqrt(reading.qps) * 0.7)))
    return Array.from({ length: count }, (_, index) => ({ curve, offset: index / count, color: healthColor[reading.health], speed: 0.075 }))
  }), [layout, environment.nodes])
  const draw = () => {
    if (!mesh.current) return
    particles.forEach((particle, index) => {
      dummy.position.copy(particle.curve.getPoint((particle.offset + clock.current * particle.speed) % 1))
      dummy.position.y += 0.07
      dummy.scale.setScalar(0.035)
      dummy.updateMatrix()
      mesh.current!.setMatrixAt(index, dummy.matrix)
      mesh.current!.setColorAt(index, new THREE.Color(particle.color))
    })
    mesh.current.count = particles.length
    mesh.current.instanceMatrix.needsUpdate = true
    if (mesh.current.instanceColor) mesh.current.instanceColor.needsUpdate = true
  }
  useEffect(draw, [particles])
  useFrame((_, delta) => {
    if (!animate) return
    clock.current += delta
    draw()
  })
  return <instancedMesh ref={mesh} args={[undefined, undefined, 500]} frustumCulled={false}>
    <sphereGeometry args={[1, 8, 6]} /><meshBasicMaterial />
  </instancedMesh>
}

function EnvironmentPlane({ environment, index, layout, scenario, workspace, animate }: {
  environment: Environment; index: number; layout: GraphLayout; scenario: Scenario; workspace: WorkspaceState; animate: boolean
}) {
  const { position, scale } = placement(index, layout)
  const group = useRef<THREE.Group>(null)
  const invalidate = useThree(state => state.invalidate)
  const targetPosition = useMemo(() => new THREE.Vector3(...position), [layout, index])
  const selectedEnvironment = useWorkspace(state => state.environmentId)
  const selectedNode = useWorkspace(state => state.selectedNode)
  const viewportWidth = useThree(state => state.size.width)
  const focus = useWorkspace(state => state.focus)
  const reducedMotion = useWorkspace(state => state.reducedMotion)
  const cursor = useWorkspace(state => state.cursor)
  const events = scenario.events.filter(event => event.at <= cursor && event.at > cursor - 6 && event.environmentId === environment.id && event.kind === 'observe')
  const actions = workspace.actions.filter(action => action.environmentId === environment.id)
  const focused = selectedEnvironment === environment.id
  useEffect(() => {
    if (group.current) group.current.position.set(position[0], reducedMotion ? position[1] : position[1] - 0.6, position[2] + (reducedMotion ? 0 : 1.2))
  }, [environment.id, layout])
  useFrame((_, delta) => {
    if (group.current && group.current.position.distanceToSquared(targetPosition) > 0.000001) {
      group.current.position.lerp(targetPosition, reducedMotion ? 1 : 1 - Math.exp(-delta * 5))
      invalidate()
    }
  })
  return <group ref={group} position={position} scale={scale}>
    <RoundedBox args={[layout.width + 1.1, 0.12, layout.height + 1.1]} radius={0.16} smoothness={3} position={[0, -0.08, 0]} receiveShadow>
      <meshStandardMaterial color={index === 0 ? '#eef2ed' : index === 1 ? '#e0ece7' : '#e9e5ef'} roughness={0.9} />
    </RoundedBox>
    <Line points={[
      [-layout.width / 2 - 0.52, 0.01, -layout.height / 2 - 0.52], [layout.width / 2 + 0.52, 0.01, -layout.height / 2 - 0.52],
      [layout.width / 2 + 0.52, 0.01, layout.height / 2 + 0.52], [-layout.width / 2 - 0.52, 0.01, layout.height / 2 + 0.52], [-layout.width / 2 - 0.52, 0.01, -layout.height / 2 - 0.52],
    ]} color={environment.color} lineWidth={focused ? 1.1 : 0.65} transparent opacity={0.35} dashed={index > 0} dashSize={0.16} gapSize={0.12} />
    <Html position={[0, 0.2, index === 0 ? layout.height / 2 + 0.9 : -layout.height / 2 - 0.9]} center zIndexRange={[35, 0]}>
      <button className={`environment-label ${focused ? 'is-focused' : ''}`} onClick={() => focus(environment.id)} style={{ '--environment-color': environment.color } as React.CSSProperties}>
        <i /><span>{environment.label}</span><small>{environment.hypothesisId ? `Hypothesis ${environment.hypothesisId}` : 'Reference system'}</small>
      </button>
    </Html>
    {layout.edges.map(edge => <group key={edge.id}>
      <Line points={edge.points} color={environment.nodes[edge.source]?.health === 'degraded' ? '#c7a58c' : '#9fb7a9'} lineWidth={1.6} />
      {edge.points.length > 1 && <mesh position={edge.points.at(-1)} rotation={[-Math.PI / 2, 0, 0]}><coneGeometry args={[0.075, 0.18, 3]} /><meshBasicMaterial color="#849d91" /></mesh>}
    </group>)}
    <Traffic layout={layout} environment={environment} animate={animate} />
    {scenario.topology.nodes.map(node => <ServiceNode key={node.id} entity={node} environment={environment} position={layout.positions[node.id]} actions={actions} showLabel={focused && (viewportWidth > 540 || node.id === scenario.targetId || node.id === selectedNode)} attention={events.some(event => event.targetId === node.id)} />)}
  </group>
}

function CameraDirector({ layout, workspace, scenario }: { layout: GraphLayout; workspace: WorkspaceState; scenario: Scenario }) {
  const controls = useRef<CameraControls>(null)
  const { size, camera } = useThree()
  const { environmentId, selectedNode, focusRevision, follow, reducedMotion, cursor, set } = useWorkspace()
  const lastEvent = useRef('')
  const move = (environment?: string, targetId?: string) => {
    if (!controls.current) return
    const index = workspace.environments.findIndex(env => env.id === environment)
    if (environment && index < 0) return
    const selected = index >= 0 ? placement(index, layout) : undefined
    const node = targetId ? layout.positions[targetId] : undefined
    const direction = new THREE.Vector3(0.22, 1.55, 1.3).normalize()
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), direction).normalize()
    const up = new THREE.Vector3().crossVectors(direction, right).normalize()
    const points: THREE.Vector3[] = []
    const planes = selected && (index > 0 || node) ? [selected] : workspace.environments.map((_, i) => placement(i, layout))
    for (const plane of planes) {
      const origin = new THREE.Vector3(...plane.position)
      if (node && selected) origin.add(new THREE.Vector3(...node).multiplyScalar(plane.scale))
      const halfWidth = node ? 2.6 : layout.width / 2 + 1
      const halfDepth = node ? 2.6 : layout.height / 2 + 1.4
      for (const x of [-halfWidth, halfWidth]) for (const z of [-halfDepth, halfDepth]) {
        points.push(origin.clone().add(new THREE.Vector3(x, 0.65, z).multiplyScalar(plane.scale)))
      }
    }
    const box = new THREE.Box3().setFromPoints(points)
    const center = box.getCenter(new THREE.Vector3())
    const fov = THREE.MathUtils.degToRad((camera as THREE.PerspectiveCamera).fov || 40)
    const tanV = Math.tan(fov / 2) * Math.max(0.65, (size.height - 95) / size.height)
    const tanH = Math.tan(fov / 2) * size.width / size.height * Math.max(0.7, (size.width - 145) / size.width)
    const distance = Math.max(5, ...points.map(point => {
      const relative = point.clone().sub(center)
      return Math.max(Math.abs(relative.dot(up)) / tanV, Math.abs(relative.dot(right)) / tanH) + relative.dot(direction)
    }))
    const position = center.clone().add(direction.multiplyScalar(distance))
    void controls.current.setLookAt(position.x, position.y, position.z, center.x, center.y, center.z, !reducedMotion)
  }
  useEffect(() => { move(environmentId, selectedNode) }, [layout, focusRevision, size.width, size.height, workspace.environments.length])
  useEffect(() => {
    if (!follow) return
    const event = scenario.events.filter(event => event.at <= cursor && (event.kind === 'clone' || event.kind === 'verdict' || (event.kind === 'action' && event.environmentId === 'production'))).at(-1)
    if (event && event.id !== lastEvent.current) {
      lastEvent.current = event.id
      move(event.environmentId, event.targetId)
    }
  }, [cursor, follow])
  return <CameraControls ref={controls} makeDefault smoothTime={0.7} minDistance={3} maxDistance={100} minPolarAngle={0.12} maxPolarAngle={Math.PI / 2.12} onControlStart={() => set({ follow: false })} />
}

class SceneBoundary extends Component<{ children: ReactNode; fallback: ReactNode }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}

export default function TopologyScene({ layout, workspace, scenario, fallback }: { layout: GraphLayout; workspace: WorkspaceState; scenario: Scenario; fallback: ReactNode }) {
  const { playing, reducedMotion } = useWorkspace()
  const [visible, setVisible] = useState(!document.hidden)
  useEffect(() => {
    const update = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', update)
    return () => document.removeEventListener('visibilitychange', update)
  }, [])
  const animate = playing && !reducedMotion && visible
  return <SceneBoundary fallback={fallback}>
    <Canvas shadows dpr={[1, 1.5]} camera={{ position: [17, 24, 28], fov: 40, near: 0.1, far: 200 }} frameloop={visible ? animate ? 'always' : 'demand' : 'never'} fallback={fallback} gl={{ antialias: true, powerPreference: 'low-power' }}>
      <color attach="background" args={['#f4f6f1']} />
      <fog attach="fog" args={['#f4f6f1', 50, 110]} />
      <ambientLight intensity={1.5} />
      <directionalLight position={[-8, 20, 7]} intensity={2.3} castShadow shadow-mapSize={[1024, 1024]} shadow-camera-left={-25} shadow-camera-right={25} shadow-camera-top={25} shadow-camera-bottom={-25} shadow-bias={-0.002} />
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.18, 0]} receiveShadow><planeGeometry args={[200, 200]} /><meshStandardMaterial color="#f4f6f1" roughness={1} /></mesh>
      <Grid position={[0, -0.17, 0]} args={[80, 80]} cellSize={0.7} cellThickness={0.4} cellColor="#dbe1d8" sectionSize={4.9} sectionThickness={0.6} sectionColor="#dbe1d8" fadeDistance={42} fadeStrength={2} />
      <Suspense fallback={null}>
        {workspace.environments.map((environment, index) => <EnvironmentPlane key={environment.id} environment={environment} index={index} layout={layout} scenario={scenario} workspace={workspace} animate={animate} />)}
      </Suspense>
      <CameraDirector layout={layout} workspace={workspace} scenario={scenario} />
    </Canvas>
  </SceneBoundary>
}
