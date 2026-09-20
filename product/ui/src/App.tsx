import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { Activity, ArrowDownRight, ArrowRight, Box, ChevronDown, ChevronRight, CircleHelp, Compass, FlaskConical, Focus, GitBranch, Layers3, LayoutDashboard, ListFilter, Maximize2, MousePointer2, Network, Pause, Plus, ShieldCheck, Sparkles, Waves } from 'lucide-react'
import { scenarios } from './scenarios'
import { replay, timeLabel, visibleEvents } from './model'
import { useWorkspace, type View } from './store'
import { useLayout } from './use-layout'
import { FlatTopology } from './components/FlatTopology'
import { Inspector } from './components/Inspector'
import { MetricChart } from './components/MetricChart'
import { Timeline } from './components/Timeline'
import { ExperimentLab, Observability, ReplayLibrary } from './components/Views'
import { Dialogs } from './components/Dialogs'

const TopologyScene = lazy(() => import('./components/TopologyScene'))
const navigation: { view: View; label: string; icon: typeof Activity }[] = [
  { view: 'observability', label: 'Observability', icon: LayoutDashboard },
  { view: 'investigation', label: 'Investigations', icon: Network },
  { view: 'experiments', label: 'Experiment lab', icon: FlaskConical },
  { view: 'replay', label: 'Replay library', icon: Layers3 },
]
const viewTitles: Record<View, { eyebrow: string; title: string; subtitle: string }> = {
  investigation: { eyebrow: 'THE INVESTIGATION WORKSPACE', title: 'Make the invisible, visible.', subtitle: 'Follow the system. Test the hypothesis. Let the evidence decide.' },
  observability: { eyebrow: 'SYSTEM OBSERVABILITY', title: 'A little less noise. A lot more signal.', subtitle: 'Metrics, dependencies, and context — all at the same moment in time.' },
  experiments: { eyebrow: 'THE CLONE LAB', title: 'Room to ask better questions.', subtitle: 'Safe, isolated environments for experiments that explain the incident.' },
  replay: { eyebrow: 'THE EVIDENCE LIBRARY', title: 'An incident becomes a test.', subtitle: 'Keep the reproduction. Carry the evidence into every future change.' },
}

function Mark() { return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span> }

export default function App() {
  const ui = useWorkspace()
  const { scenarioId, cursor, playing, view, environmentId, selectedNode, flat, follow, reducedMotion, set, focus, inspect, setScenario } = ui
  const scenario = scenarios.find(item => item.id === scenarioId)!
  const sampleTime = Math.floor(cursor)
  const workspace = useMemo(() => replay(scenario, sampleTime), [scenario, sampleTime])
  const events = useMemo(() => visibleEvents(scenario, sampleTime), [scenario, sampleTime])
  const environment = workspace.environments.find(env => env.id === environmentId) ?? workspace.environments[0]
  const { layout, error } = useLayout(scenario.topology)
  const [entitySearch, setEntitySearch] = useState('')
  const [showEntities, setShowEntities] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const reading = environment.nodes[scenario.targetId]
  const title = viewTitles[view]
  const activeActions = workspace.actions.filter(action => action.status !== 'reverted')
  const targetName = scenario.topology.nodes.find(node => node.id === scenario.targetId)!.label
  const latest = events.at(-1)
  const phases = [...new Set(scenario.events.flatMap(event => event.phase ? [event.phase] : []))]
  const phaseNumber = phases.indexOf(workspace.phase) + 1

  useEffect(() => {
    let last = performance.now()
    const resetClock = () => { last = performance.now() }
    const id = window.setInterval(() => {
      const now = performance.now()
      if (!document.hidden) useWorkspace.getState().tick(Math.min((now - last) / 1000, 0.5))
      last = now
    }, 100)
    document.addEventListener('visibilitychange', resetClock)
    return () => { clearInterval(id); document.removeEventListener('visibilitychange', resetClock) }
  }, [])
  useEffect(() => {
    if (!workspace.environments.some(env => env.id === environmentId)) focus('production')
  }, [workspace.environments.length, environmentId])
  useEffect(() => {
    const media = window.matchMedia('(prefers-reduced-motion: reduce)')
    const change = () => set({ reducedMotion: media.matches })
    media.addEventListener('change', change)
    return () => media.removeEventListener('change', change)
  }, [])
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setExpanded(false); setShowEntities(false) } }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [])

  const fallback = <FlatTopology scenario={scenario} layout={layout} environment={environment} />
  return <div className="app-shell">
    <a className="skip-link" href="#main-content">Skip to workspace</a>
    <aside className="sidebar">
      <a className="brand" aria-label="Faultline workspace" href="#main-content" onClick={() => set({ view: 'investigation' })}><Mark /><span>faultline<span className="brand-period">.</span></span></a>
      <div className="workspace-switch"><span className="workspace-avatar">FL</span><div><strong>Faultline workspace</strong><small>Design environment</small></div><ChevronDown size={13} /></div>
      <span className="nav-group-label">WORKSPACE</span>
      <nav aria-label="Main navigation">{navigation.map(item => <button key={item.view} aria-label={item.label} className={`nav-item ${view === item.view ? 'active' : ''}`} onClick={() => set({ view: item.view })} aria-current={view === item.view ? 'page' : undefined}><item.icon size={17} strokeWidth={1.65} /><span>{item.label}</span>{item.view === 'investigation' && <span className="nav-count">1</span>}</button>)}</nav>
      <div className="sidebar-rule" /><span className="nav-group-label">GUARDRAILS</span>
      <button className="nav-item" aria-label="Safety & approvals" onClick={() => set({ dialog: 'safety' })}><ShieldCheck size={17} strokeWidth={1.65} /><span>Safety & approvals</span><span className="approval-dot" /></button>
      <div className="sidebar-note"><span className="small-icon"><GitBranch size={17} /></span><strong>Try a different architecture.</strong><p>The map is derived from data, not drawn for one system.</p><button onClick={() => setScenario(scenarioId === 'commerce' ? 'pipeline' : 'commerce')}>Switch example <ArrowRight size={13} /></button></div>
      <div className="sidebar-bottom"><span className="preview-indicator"><i />DESIGN PREVIEW</span><p>Simulated data. Real possibilities.</p><button className="sidebar-help" onClick={() => set({ dialog: 'safety' })}><CircleHelp size={15} />About this prototype<ArrowRight size={13} /></button><div className="profile"><span className="profile-avatar">FL</span><div><strong>Local workspace</strong><small>No live connection</small></div><span className="offline-dot" /></div></div>
    </aside>

    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><ChevronRight size={12} /><strong>{navigation.find(item => item.view === view)?.label}</strong></div><div className="topbar-actions"><span className="demo-badge"><i />Simulated data</span><span className="topbar-divider" /><button className="pause-all" onClick={() => set({ playing: false })} disabled={!playing}><Pause size={13} />Pause simulation</button></div></header>
      <main id="main-content">
        <section className="page-heading"><div><span className="overline">{title.eyebrow}</span><h1>{title.title}</h1><p>{title.subtitle}</p></div><button className="primary-button" onClick={() => set({ dialog: 'experiment' })}><Plus size={15} />New experiment</button></section>
        <div className="context-bar"><div className="scenario-context"><Box size={15} /><select aria-label="Example architecture" value={scenarioId} onChange={event => { setScenario(event.target.value); setEntitySearch('') }}>{scenarios.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select><span className="context-separator" /><span className="context-type">{scenario.subtitle}</span></div><div className="environment-context"><span>Environment</span><select aria-label="Selected environment" value={environment.id} onChange={event => focus(event.target.value)}>{workspace.environments.map(env => <option key={env.id} value={env.id}>{env.label}</option>)}</select></div></div>

        <section className="metric-strip" aria-label="Current illustrative metrics">
          <div className="metric-summary"><span><Network size={13} />TOPOLOGY</span><div>{scenario.topology.nodes.length}<small>entities</small><b>{scenario.topology.edges.length} connections</b></div></div>
          <div className="metric-summary"><span><Waves size={13} />{targetName.toUpperCase()} · P99</span><div className={reading?.health === 'degraded' ? 'metric-warning' : ''}>{reading?.latency === undefined ? '—' : new Intl.NumberFormat('en-US').format(reading.latency)}<small>ms</small><b>{reading?.health === 'degraded' ? 'Above reference' : reading?.health === 'healthy' ? 'Within reference' : 'Not collected'}</b></div></div>
          <div className="metric-summary"><span><GitBranch size={13} />ISOLATED ENVIRONMENTS</span><div>{workspace.environments.length - 1}<small>clones</small><b>Production isolated</b></div></div>
          <div className="metric-summary"><span><ShieldCheck size={13} />PRODUCTION ACTIONS</span><div>{workspace.actions.filter(action => action.environmentId === 'production').length}<small>/ 5 budget</small><b>{activeActions.filter(action => action.environmentId === 'production').length ? 'Bounded by TTL' : 'No active intervention'}</b></div></div>
        </section>

        {view === 'investigation' ? <>
          <div className="incident-banner"><span className={`incident-symbol ${cursor < 12 || workspace.verdict ? 'resolved' : ''}`}><Activity size={15} /></span><div><strong>{cursor < 12 ? 'Establishing a healthy reference' : workspace.verdict ?? scenario.incidentTitle}</strong><span>{scenario.incident} <span>·</span> {workspace.phase}</span></div><span className="incident-stage">{String(phaseNumber).padStart(2, '0')} <span>/ {String(phases.length).padStart(2, '0')}</span></span><div className="stage-progress">{Array.from({ length: phases.length }, (_, i) => <i key={i} className={i < phaseNumber ? 'filled' : ''} />)}</div></div>
          <div className="workspace-grid">
            <section className={`panel topology-panel ${expanded ? 'is-expanded' : ''}`}>
              <div className="map-heading"><div><span className="overline">SYSTEM TOPOLOGY</span><span className="map-subtitle">One system. Parallel possibilities.</span></div><div className="map-toolbar"><div className="segmented"><button aria-label="Show 3D topology" aria-pressed={!flat} onClick={() => set({ flat: false })}>3D</button><button aria-label="Show flat topology" aria-pressed={flat} onClick={() => set({ flat: true })}>2D</button></div><button className="icon-button" aria-label="Find an entity" aria-expanded={showEntities} onClick={() => setShowEntities(!showEntities)}><ListFilter size={15} /></button><button className="icon-button" aria-label="Fit whole system" onClick={() => { focus('production'); set({ selectedNode: undefined }) }}><Focus size={16} /></button><button className="icon-button" aria-label={expanded ? 'Exit expanded map' : 'Expand map'} onClick={() => setExpanded(!expanded)}><Maximize2 size={15} /></button></div></div>
              <div className="map-canvas" data-testid="topology-stage">
                {error ? <div className="layout-error">{error}{fallback}</div> : flat ? fallback : layout ? <Suspense fallback={<div className="scene-loading"><Network size={25} /><span>Preparing the spatial workspace…</span></div>}><TopologyScene layout={layout} workspace={workspace} scenario={scenario} fallback={fallback} /></Suspense> : <div className="scene-loading"><Network size={25} /><span>Mapping dependencies…</span></div>}
                <div className="map-legend"><span><i className="health-dot healthy" />Healthy</span><span><i className="health-dot degraded" />Degraded</span><span><i className="health-dot unknown" />Unknown</span></div>
                {showEntities && <div className="entity-picker"><label htmlFor="entity-search">Find & inspect an entity</label><input id="entity-search" autoFocus placeholder="Service or dependency…" value={entitySearch} onChange={event => setEntitySearch(event.target.value)} />{scenario.topology.nodes.filter(node => node.label.toLowerCase().includes(entitySearch.toLowerCase())).map(node => <button key={node.id} onClick={() => { inspect(node.id, environment.id); setShowEntities(false) }}><Box size={13} />{node.label}<ArrowRight size={12} /></button>)}</div>}
                <div className="map-status"><span className={`map-mode ${playing ? 'is-playing' : ''}`}><i />{playing ? 'SIMULATION PLAYING' : 'REPLAY PAUSED'}</span><span>Traffic is illustrative, not individual requests</span></div>
                <div className="camera-tools"><button className={follow ? 'active' : ''} aria-pressed={follow} onClick={() => set({ follow: !follow })}><Compass size={13} />{follow ? 'Following key events' : 'Follow key events'}</button><button aria-pressed={reducedMotion} onClick={() => set({ reducedMotion: !reducedMotion })}><MousePointer2 size={12} />{reducedMotion ? 'Reduced motion' : 'Full motion'}</button></div>
              </div>
              <div className="map-environments">{workspace.environments.map(env => <button className={environment.id === env.id ? 'selected' : ''} key={env.id} onClick={() => focus(env.id)}><span style={{ background: env.color }} /><strong>{env.label}</strong><small>{env.hypothesisId ? `Hypothesis ${env.hypothesisId}` : 'Reference'}</small>{env.id !== 'production' && <GitBranch size={12} />}</button>)}</div>
              <Timeline scenario={scenario} />
            </section>
            <Inspector scenario={scenario} workspace={workspace} environment={environment} />
          </div>
          <div className="below-stage"><section className="panel pulse-chart"><div className="panel-title"><div><span className="overline">THE RESPONSE, IN CONTEXT</span><h3>{selectedNode ?? targetName} <span className="muted">/ {environment.label}</span></h3></div><button className="icon-button" aria-label="Open observability dashboard" onClick={() => set({ view: 'observability' })}><ArrowRight size={15} /></button></div><div className="chart-legend"><i className="latency-line" />Latency · ms<i className="load-line" />Issued load · qps</div><MetricChart scenario={scenario} cursor={cursor} environmentId={environment.id} nodeId={selectedNode ?? scenario.targetId} compact /></section><section className="panel decision-summary"><span className="overline"><Sparkles size={12} />LATEST EVIDENCE</span><h3>{latest?.title}</h3><p>{latest?.detail}</p><button className="text-button" onClick={() => set({ traceTab: 'trace', selectedEvent: latest?.id })}>See the decision trace <ArrowRight size={13} /></button><div className="decision-meta"><span>{latest?.environmentId}</span><span>{timeLabel(latest?.at ?? 0)}</span><span>Scripted example</span></div></section></div>
        </> : <>
          {view === 'observability' && <Observability scenario={scenario} environment={environment} />}
          {view === 'experiments' && <ExperimentLab scenario={scenario} workspace={workspace} />}
          {view === 'replay' && <ReplayLibrary scenario={scenario} />}
          <div className="panel page-timeline"><Timeline scenario={scenario} /></div>
        </>}
        <footer className="page-footer"><span><Mark />Faultline <span>·</span> The model proposes. Measurement decides.</span><span>Interactive design prototype <ArrowDownRight size={12} /></span></footer>
      </main>
    </div>
    <Dialogs scenario={scenario} workspace={workspace} />
  </div>
}
