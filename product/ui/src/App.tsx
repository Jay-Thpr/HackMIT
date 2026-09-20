import { lazy, Suspense, useEffect, useMemo, useState } from 'react'
import { Activity, ArrowDownRight, ArrowRight, Box, ChevronDown, ChevronRight, CircleHelp, Compass, FlaskConical, Focus, GitBranch, Layers3, LayoutDashboard, ListFilter, Maximize2, MousePointer2, Network, Pause, Play, Plus, ShieldCheck, Sparkles, Waves } from 'lucide-react'
import { followIncident, loadLiveScenarios } from './live'
import { replay, timeLabel, visibleEvents } from './model'
import { useWorkspace, type View } from './store'
import { useLayout } from './use-layout'
import { Inspector } from './components/Inspector'
import { MetricChart } from './components/MetricChart'
import { Timeline } from './components/Timeline'
import { ExperimentLab, Observability, ReplayLibrary } from './components/Views'
import { Explanation } from './components/Explanation'
import { Dialogs } from './components/Dialogs'

const TopologyScene = lazy(() => import('./components/TopologyScene'))
const navigation: { view: View; label: string; icon: typeof Activity }[] = [
  { view: 'explanation', label: 'Why this incident?', icon: CircleHelp },
  { view: 'observability', label: 'Observability', icon: LayoutDashboard },
  { view: 'investigation', label: 'Agent workspace', icon: Network },
  { view: 'experiments', label: 'Experiment lab', icon: FlaskConical },
  { view: 'replay', label: 'Replay library', icon: Layers3 },
]
const viewTitles: Record<View, { eyebrow: string; title: string; subtitle: string }> = {
  explanation: { eyebrow: 'UNDERSTAND THE INCIDENT', title: 'Why this incident?', subtitle: 'The symptoms, the possible causes, and the tests that tell them apart.' },
  investigation: { eyebrow: 'THE INVESTIGATION WORKSPACE', title: 'Investigation workspace', subtitle: 'Trace the symptoms. Test in isolation. Follow the evidence.' },
  observability: { eyebrow: 'SYSTEM OBSERVABILITY', title: 'Observability', subtitle: 'Metrics, dependencies, and context at the same moment in time.' },
  experiments: { eyebrow: 'THE CLONE LAB', title: 'Experiment lab', subtitle: 'Safe, isolated environments for experiments that explain the incident.' },
  replay: { eyebrow: 'THE EVIDENCE LIBRARY', title: 'Replay library', subtitle: 'Keep the reproduction. Carry the evidence into every future change.' },
}

function Mark() { return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span> }

export default function App() {
  const ui = useWorkspace()
  const { scenarios, scenarioId, cursor, playing, view, environmentId, isolatedLayer, selectedNode, follow, reducedMotion, set, focus, inspect, setScenario } = ui
  const scenario = scenarios.find(item => item.id === scenarioId) ?? scenarios[0]
  const sampleTime = Math.floor(cursor)
  const workspace = useMemo(() => replay(scenario, sampleTime), [scenario, sampleTime])
  const events = useMemo(() => visibleEvents(scenario, sampleTime), [scenario, sampleTime])
  const environment = workspace.environments.find(env => env.id === environmentId) ?? workspace.environments[0]
  const { layout, error } = useLayout(scenario.topology)
  const [entitySearch, setEntitySearch] = useState('')
  const [showEntities, setShowEntities] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const showInspector = Boolean(selectedNode) || Boolean(ui.selectedSuiteCheck) || evidenceOpen
  const reading = environment.nodes[scenario.targetId]
  const title = viewTitles[view]
  const activeActions = workspace.actions.filter(action => action.status !== 'reverted')
  const targetName = scenario.topology.nodes.find(node => node.id === scenario.targetId)!.label
  const latest = events.at(-1)
  const phases = [...new Set(scenario.events.flatMap(event => event.phase ? [event.phase] : []))]
  const phaseNumber = phases.indexOf(workspace.phase) + 1

  useEffect(() => { void loadLiveScenarios() }, [])
  useEffect(() => { if (scenario.live && !scenario.complete) followIncident(scenario.id) }, [scenario.id, scenario.live, scenario.complete])

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
    if (!workspace.environments.some(env => env.id === environmentId)) { focus('production'); set({ isolatedLayer: null }) }
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

  const fallback = <div className="scene-unavailable"><h3>3D view unavailable</h3><p>You can still inspect each system and its recorded activity.</p>{scenario.topology.nodes.map(node => <button key={node.id} onClick={() => inspect(node.id, environment.id)}>{node.label}</button>)}</div>
  return <div className={`app-shell ${view === 'investigation' ? 'is-spatial-workspace' : ''}`}>
    <a className="skip-link" href="#main-content">Skip to workspace</a>
    <aside className="sidebar">
      <a className="brand" aria-label="Faultline workspace" href="#main-content" onClick={() => set({ view: 'investigation' })}><Mark /><span>faultline<span className="brand-period">.</span></span></a>
      <span className="nav-group-label">WORKSPACE</span>
      <nav aria-label="Main navigation">{navigation.map(item => <button key={item.view} aria-label={item.label} className={`nav-item ${view === item.view ? 'active' : ''}`} onClick={() => set({ view: item.view })} aria-current={view === item.view ? 'page' : undefined}><item.icon size={17} strokeWidth={1.65} /><span>{item.label}</span>{item.view === 'investigation' && <span className="nav-count">1</span>}</button>)}</nav>
      <div className="sidebar-rule" /><span className="nav-group-label">GUARDRAILS</span>
      <button className="nav-item" aria-label="Safety & approvals" onClick={() => set({ dialog: 'safety' })}><ShieldCheck size={17} strokeWidth={1.65} /><span>Safety & approvals</span><span className="approval-dot" /></button>
      <div className="sidebar-case"><span className="nav-group-label">CURRENT CASE</span><button onClick={() => set({ view: 'investigation' })}><span>{scenario.incident}</span><strong>{scenario.name}</strong><small>{workspace.phase}</small></button></div>
      <div className="sidebar-bottom"><span className="preview-indicator"><i />DESIGN PREVIEW</span><p>A safe space to investigate.</p><button className="sidebar-help" onClick={() => set({ dialog: 'safety' })}><CircleHelp size={15} />About this prototype<ArrowRight size={13} /></button><div className="profile"><span className="profile-avatar">FL</span><div><strong>Local workspace</strong><small>No live connection</small></div><span className="offline-dot" /></div></div>
    </aside>

    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><ChevronRight size={12} /><strong>{navigation.find(item => item.view === view)?.label}</strong></div><div className="topbar-actions"><span className="demo-badge"><i />{scenario.live ? (scenario.complete ? 'Live incident' : 'Live incident · in progress') : 'Simulated data'}</span><span className="topbar-divider" /><button className="pause-all" onClick={() => set({ playing: false })} disabled={!playing}><Pause size={13} />Pause simulation</button></div></header>
      <main id="main-content">
        <section className="workspace-header"><div><div className="workspace-brief"><span className="case-id">{scenario.incident}</span><span className="case-state"><i />{workspace.phase}</span></div><h1>{view === 'investigation' ? (cursor < 12 ? 'Establishing a healthy reference' : workspace.verdict ?? scenario.incidentTitle) : title.title}</h1>{view !== 'investigation' && <p>{title.subtitle}</p>}</div><div className="workspace-header-actions">{view === 'investigation' && <button className="primary-button" onClick={() => { ui.togglePlay(); set({ follow: true }) }}>{playing ? <Pause size={14} /> : <Play size={14} />}{playing ? 'Pause investigation' : 'Follow investigation'}</button>}<button className="secondary-button" onClick={() => set({ dialog: 'experiment' })}><Plus size={15} />New experiment</button></div></section>
        <div className="context-bar"><div className="scenario-context"><Box size={15} /><select aria-label="Example architecture" value={scenarioId} onChange={event => { setScenario(event.target.value); setEntitySearch('') }}>{scenarios.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select><span className="context-separator" /><span className="context-type">{scenario.subtitle}</span></div><div className="environment-context"><span>Environment</span><select aria-label="Selected environment" value={environment.id} onChange={event => focus(event.target.value)}>{workspace.environments.map(env => <option key={env.id} value={env.id}>{env.label}</option>)}</select></div></div>

        <section className="workspace-facts" aria-label="Current illustrative metrics"><span><Network size={13} /><strong>{scenario.topology.nodes.length}</strong> entities <span className="fact-muted">/ {scenario.topology.edges.length} connections</span></span><span><Waves size={13} />{targetName} <strong className={reading?.health === 'degraded' ? 'metric-warning' : ''}>{reading?.latency === undefined ? 'Not collected' : `${new Intl.NumberFormat('en-US').format(reading.latency)} ms`}</strong></span><span><GitBranch size={13} /><strong>{workspace.environments.length - 1}</strong> isolated clones</span><span><ShieldCheck size={13} /><strong>{workspace.actions.filter(action => action.environmentId === 'production').length} / 5</strong> actions <span className="fact-muted">{activeActions.some(action => action.environmentId === 'production') ? '· TTL bounded' : '· No active intervention'}</span></span></section>

        {view === 'investigation' ? <>
          <details className="phase-disclosure"><summary>Investigation steps <span>{phaseNumber} / {phases.length}</span></summary><nav className="investigation-path" aria-label="Investigation phases">{phases.map((phase, index) => {
            const event = scenario.events.find(item => item.phase === phase)!
            return <button key={phase} className={`path-step ${index < phaseNumber - 1 ? 'complete' : ''} ${phase === workspace.phase ? 'current' : ''}`} aria-current={phase === workspace.phase ? 'step' : undefined} onClick={() => ui.seek(event.at)} title={`Jump to ${phase} at ${timeLabel(event.at)}`}><span className="path-marker">{index + 1}</span><span>{phase}</span></button>
          })}</nav></details>
          <div className={`workspace-grid ${showInspector ? 'has-inspector' : 'without-inspector'} ${expanded ? 'is-expanded' : ''}`}>
            <section className="panel topology-panel">
              <div className="map-heading"><div><span className="map-instruction">Select a node to inspect the investigation</span></div><div className="map-toolbar"><button className="evidence-toggle" onClick={() => set({ view: 'explanation' })}>Why this incident?</button><button className="evidence-toggle" aria-expanded={showInspector} onClick={() => { setEvidenceOpen(!showInspector); if (showInspector) set({ selectedNode: undefined, selectedSuiteCheck: undefined }) }}>{showInspector ? 'Close evidence' : 'Open evidence'}</button><button className="icon-button" aria-label="Find an entity" aria-expanded={showEntities} onClick={() => setShowEntities(!showEntities)}><ListFilter size={15} /></button><button className="icon-button" aria-label="Fit whole system" onClick={() => { focus('production'); set({ selectedNode: undefined, isolatedLayer: null }); setEvidenceOpen(false) }}><Focus size={16} /></button><button className="icon-button" aria-label={expanded ? 'Exit expanded map' : 'Expand map'} onClick={() => setExpanded(!expanded)}><Maximize2 size={15} /></button></div></div>
              <div className="map-canvas" data-testid="topology-stage" data-isolated-layer={isolatedLayer ?? 'all'}>
                {error ? <div className="layout-error">{error}{fallback}</div> : layout ? <Suspense fallback={<div className="scene-loading"><Network size={25} /><span>Preparing the spatial workspace…</span></div>}><TopologyScene layout={layout} workspace={workspace} scenario={scenario} fallback={fallback} /></Suspense> : <div className="scene-loading"><Network size={25} /><span>Mapping dependencies…</span></div>}
                <div className="map-legend"><span><i className="scene-key issue" />Issue</span><span><i className="scene-key attention" />Recovering</span><span><i className="scene-key infrastructure" />Healthy</span></div>
                {showEntities && <div className="entity-picker"><label htmlFor="entity-search">Find & inspect an entity</label><input id="entity-search" autoFocus placeholder="Service or dependency…" value={entitySearch} onChange={event => setEntitySearch(event.target.value)} />{scenario.topology.nodes.filter(node => node.label.toLowerCase().includes(entitySearch.toLowerCase())).map(node => <button key={node.id} onClick={() => { inspect(node.id, environment.id); set({ traceTab: 'trace', follow: false }); setShowEntities(false) }}><Box size={13} />{node.label}<ArrowRight size={12} /></button>)}</div>}
                <div className="map-status"><span className={`map-mode ${playing ? 'is-playing' : ''}`}><i />{playing ? 'SIMULATION PLAYING' : 'REPLAY PAUSED'}</span><span>{scenario.live ? 'Readings are 5 s C1 windows; traffic animation is illustrative' : 'Traffic is illustrative, not individual requests'}</span></div>
                <div className="camera-tools"><button className={follow ? 'active' : ''} aria-pressed={follow} onClick={() => set({ follow: !follow })}><Compass size={13} />{follow ? 'Following key events' : 'Follow key events'}</button><button aria-pressed={reducedMotion} onClick={() => set({ reducedMotion: !reducedMotion })}><MousePointer2 size={12} />{reducedMotion ? 'Reduced motion' : 'Full motion'}</button></div>
              </div>
              <div className="map-environments" aria-label="System layers"><button className={isolatedLayer === null ? 'selected' : ''} aria-pressed={isolatedLayer === null} onClick={() => { focus('production'); set({ isolatedLayer: null }); setEvidenceOpen(false) }}><Layers3 size={13} /><strong>All layers</strong></button>{workspace.environments.map((env, index) => <button className={isolatedLayer === env.id ? 'selected' : ''} aria-label={`Focus ${env.label} layer`} aria-pressed={isolatedLayer === env.id} key={env.id} onClick={() => focus(env.id)}><span style={{ background: env.color }} /><strong>{env.label}</strong><small>{index === 0 ? 'Base level' : `Level ${index}`}</small>{env.id !== 'production' && <GitBranch size={12} />}</button>)}</div>
              <Timeline scenario={scenario} />
            </section>
            {showInspector && <Inspector scenario={scenario} workspace={workspace} environment={environment} />}
          </div>
          {showInspector && <div className="below-stage"><section className="panel pulse-chart"><div className="panel-title"><div><span className="overline">THE RESPONSE, IN CONTEXT</span><h3>{selectedNode ?? targetName} <span className="muted">/ {environment.label}</span></h3></div><button className="icon-button" aria-label="Open observability dashboard" onClick={() => set({ view: 'observability' })}><ArrowRight size={15} /></button></div><div className="chart-legend"><i className="latency-line" />Latency · ms<i className="load-line" />Issued load · qps</div><MetricChart scenario={scenario} cursor={cursor} environmentId={environment.id} nodeId={selectedNode ?? scenario.targetId} compact /></section><section className="panel decision-summary"><span className="overline"><Sparkles size={12} />LATEST EVIDENCE</span><h3>{latest?.title}</h3><p>{latest?.detail}</p><button className="text-button" onClick={() => set({ traceTab: 'trace', selectedEvent: latest?.id })}>See the decision trace <ArrowRight size={13} /></button><div className="decision-meta"><span>{latest?.environmentId}</span><span>{timeLabel(latest?.at ?? 0)}</span><span>Scripted example</span></div></section></div>}
        </> : <>
          {view === 'explanation' && <Explanation scenario={scenario} workspace={workspace} />}
          {view === 'observability' && <Observability scenario={scenario} environment={environment} />}
          {view === 'experiments' && <ExperimentLab scenario={scenario} workspace={workspace} />}
          {view === 'replay' && <ReplayLibrary scenario={scenario} />}
          <div className="panel page-timeline"><Timeline scenario={scenario} /></div>
        </>}
        <footer className="page-footer"><span><Mark />Faultline <span>·</span> The model proposes. Measurement decides.</span><span>{scenario.live ? 'Real audit log · readings from Elasticsearch' : <>Interactive design prototype <ArrowDownRight size={12} /></>}</span></footer>
      </main>
    </div>
    <Dialogs scenario={scenario} workspace={workspace} />
  </div>
}
