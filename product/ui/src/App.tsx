import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, ArrowDownRight, ArrowRight, Box, ChevronDown, ChevronRight, CircleHelp, Compass, FlaskConical, Focus, GitBranch, Layers3, LayoutDashboard, ListFilter, Maximize2, MousePointer2, Network, Pause, Play, Plus, ShieldCheck, Sparkles, Waves } from 'lucide-react'
import { followIncident, loadLiveScenarios } from './live'
import { exportEvidence } from './evidence'
import { environmentLifecycleLabel, environmentOutcomeLabel, replay, timeLabel, visibleEvents, type IncidentLifecycle } from './model'
import { useWorkspace, type View } from './store'
import { useLayout } from './use-layout'
import { Inspector } from './components/Inspector'
import { MetricChart } from './components/MetricChart'
import { Timeline } from './components/Timeline'
import { ElasticLineage, ExperimentLab, Observability, ReplayLibrary } from './components/Views'
import { Explanation } from './components/Explanation'
import { Dialogs } from './components/Dialogs'
import { ComparisonReplay } from './comparison'

const TopologyScene = lazy(() => import('./components/TopologyScene'))
const navigation: { view: View; label: string; icon: typeof Activity }[] = [
  { view: 'explanation', label: 'Why this incident?', icon: CircleHelp },
  { view: 'observability', label: 'Observability', icon: LayoutDashboard },
  { view: 'investigation', label: 'Agent workspace', icon: Network },
  { view: 'experiments', label: 'Clone experiments', icon: FlaskConical },
  { view: 'replay', label: 'Incident replay', icon: Layers3 },
  { view: 'elastic', label: 'Evidence lineage', icon: GitBranch },
  { view: 'comparison', label: 'Compare responders', icon: GitBranch },
]
const viewTitles: Record<View, { eyebrow: string; title: string; subtitle: string }> = {
  explanation: { eyebrow: 'UNDERSTAND THE INCIDENT', title: 'Why this incident?', subtitle: 'The symptoms, the possible causes, and the tests that tell them apart.' },
  investigation: { eyebrow: 'THE INVESTIGATION WORKSPACE', title: 'Investigation workspace', subtitle: 'Trace the symptoms. Test in isolation. Follow the evidence.' },
  observability: { eyebrow: 'SYSTEM OBSERVABILITY', title: 'Observability', subtitle: 'Metrics, dependencies, and context at the same moment in time.' },
  experiments: { eyebrow: 'THE CLONE LAB', title: 'Clone experiments', subtitle: 'Inspect what each isolated copy is testing, or draft a new test without running it.' },
  replay: { eyebrow: 'THE EVIDENCE LIBRARY', title: 'Incident replay', subtitle: 'Choose a recorded demo to watch again. Playback never reruns infrastructure actions.' },
  elastic: { eyebrow: 'BUILT WITH ELASTIC', title: 'Evidence lineage', subtitle: 'How telemetry becomes a bounded, inspectable decision.' },
  comparison: { eyebrow: 'RESPONDER COMPARISON', title: 'Compare responders', subtitle: 'Replay recorded runs side by side. Development-set comparison, not a held-out benchmark.' },
}
const lifecycleSteps: { id: IncidentLifecycle; label: string; title: string; detail: string }[] = [
  { id: 'monitoring', label: 'Normal system', title: 'Healthy reference system', detail: 'Monitored services are at baseline. Play the demo to follow an incident from its first symptoms to cleanup.' },
  { id: 'detected', label: 'Issue detected', title: 'An incident has been detected', detail: 'Latency and errors have risen. The agent is comparing possible causes before starting isolated test environments.' },
  { id: 'starting', label: 'Start clones', title: 'Starting clean copies of the system', detail: 'Each clone is an isolated test environment. It starts without the production fault and must pass a readiness check before testing.' },
  { id: 'investigating', label: 'Test in clones', title: 'Investigating in isolated clones', detail: 'Agents reproduce each possible cause, apply a reversible probe, and compare the measured responses. Production stays separate.' },
  { id: 'confirming', label: 'Confirm response', title: 'Checking the production response', detail: 'A short, reversible retry cap tests the predictions. Recovery must persist after release before the agent can confirm a cause.' },
  { id: 'cleanup', label: 'Remove clones', title: 'Removing temporary test environments', detail: 'Clone projects are torn down once their removal is recorded; the clones stay in the workspace, faded, with their observations and test results.' },
  { id: 'complete', label: 'Review evidence', title: 'Investigation complete', detail: 'The clones stay for review; the one that carried the confirmed cause (or the verified fix) is emphasised. Open the report, inspect the retained tests, or replay from the beginning.' },
]

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
  const hasHypotheses = events.some(event => event.kind === 'reason')
  const questionLabel = hasHypotheses ? `${scenario.hypotheses.length} possible causes` : workspace.lifecycle === 'monitoring' ? 'Healthy reference' : 'Cause not proposed yet'
  const latest = events.at(-1)
  const phases = [...new Set(scenario.events.flatMap(event => event.phase ? [event.phase] : []))]
  const phaseNumber = phases.indexOf(workspace.phase) + 1
  const lifecycleIndex = lifecycleSteps.findIndex(step => step.id === workspace.lifecycle)
  const lifecycleStep = lifecycleSteps[lifecycleIndex]
  const playbackLabel = scenario.live ? (playing ? 'Pause replay' : 'Play incident replay') : playing ? 'Pause demo' : workspace.lifecycle === 'complete' || cursor >= scenario.duration ? 'Replay incident demo' : cursor === 0 ? 'Play incident demo' : 'Resume demo'
  const toggleDemo = () => {
    if (!playing && (cursor === 0 || workspace.lifecycle === 'complete' || cursor >= scenario.duration)) { setEvidenceOpen(false); ui.startDemo(); return }
    ui.togglePlay()
  }

  useEffect(() => { void loadLiveScenarios() }, [])
  // The report opens by itself when the investigation reaches its end while it is being watched
  // (playback or a live incident streaming in) — not when someone merely seeks to the end.
  const wasComplete = useRef(workspace.lifecycle === 'complete')
  useEffect(() => {
    const complete = workspace.lifecycle === 'complete'
    if (complete && !wasComplete.current && (playing || ui.streaming === scenario.id) && view === 'investigation') set({ dialog: 'report', playing: false })
    wasComplete.current = complete
  }, [workspace.lifecycle, scenario.id])
  useEffect(() => { wasComplete.current = replay(scenario, cursor).lifecycle === 'complete' }, [scenario.id])
  useEffect(() => { if (scenario.live && !scenario.complete) return followIncident(scenario.id) }, [scenario.id, scenario.live, scenario.complete])

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
    if (!workspace.environments.some(env => env.id === environmentId)) { focus('production'); set({ isolatedLayer: null, follow }); setEvidenceOpen(false) }
  }, [workspace.environments.length, environmentId])
  useEffect(() => { if (cursor === 0) { setEvidenceOpen(false); setShowEntities(false) } }, [scenario.id, cursor === 0])
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
      <nav aria-label="Main navigation">{navigation.map(item => <button key={item.view} aria-label={item.label} className={`nav-item ${view === item.view ? 'active' : ''}`} onClick={() => set({ view: item.view })} aria-current={view === item.view ? 'page' : undefined}><item.icon size={17} strokeWidth={1.65} /><span>{item.label}</span></button>)}</nav>
      <div className="sidebar-rule" /><span className="nav-group-label">GUARDRAILS</span>
      <button className="nav-item" aria-label="Safety & approvals" onClick={() => set({ dialog: 'safety' })}><ShieldCheck size={17} strokeWidth={1.65} /><span>Safety & approvals</span><span className="approval-dot" /></button>
      <div className="sidebar-case"><span className="nav-group-label">CURRENT CASE</span><button onClick={() => set({ view: 'investigation' })}><span>{scenario.incident}</span><strong>{scenario.name}</strong><small>{workspace.phase}</small></button></div>
      <div className="sidebar-bottom"><span className="preview-indicator" data-live={scenario.live || undefined}><i />{scenario.live ? 'RECORDED RUN' : 'DESIGN PREVIEW'}</span><p>{scenario.live ? 'Replaying a recorded investigation.' : 'A safe space to investigate.'}</p><button className="sidebar-help" onClick={() => set({ dialog: 'safety' })}><CircleHelp size={15} />{scenario.live ? 'Safety and approvals' : 'About this prototype'}<ArrowRight size={13} /></button><div className="profile"><span className="profile-avatar">FL</span><div><strong>Local workspace</strong><small>{scenario.live ? 'Recorded locally' : 'No live connection'}</small></div><span className={scenario.live ? 'live-dot' : 'offline-dot'} /></div></div>
    </aside>

    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><ChevronRight size={12} /><strong>{navigation.find(item => item.view === view)?.label}</strong></div><div className="topbar-actions"><span className="demo-badge"><i />{view === 'comparison' ? 'Recorded comparison' : scenario.live ? (scenario.complete ? 'Live incident' : 'Live incident · in progress') : 'Simulated data'}</span>{view !== 'comparison' && <>{scenario.live && (scenario.source === 'api'
                  ? <a className="pause-all" href={`/api/incidents/${encodeURIComponent(scenario.id)}/evidence.json`} download>Export evidence</a>
                  : <button className="pause-all" onClick={() => exportEvidence(scenario)}>Export evidence</button>)}<span className="topbar-divider" /><button className="pause-all" onClick={() => set({ playing: false })} disabled={!playing}><Pause size={13} />Pause simulation</button></>}</div></header>
      <main id="main-content">
        <section className="workspace-header"><div><div className="workspace-brief"><span className="case-id">{scenario.incident}</span><span className="case-state" data-lifecycle={workspace.lifecycle}><i />{workspace.phase}</span></div><h1>{view === 'investigation' ? (workspace.lifecycle === 'monitoring' ? 'Healthy reference system' : workspace.verdict ?? scenario.incidentTitle) : title.title}</h1>{view !== 'investigation' && <p>{title.subtitle}</p>}</div><div className="workspace-header-actions">{view === 'investigation' && <button className="primary-button" onClick={toggleDemo}>{playing ? <Pause size={14} /> : <Play size={14} />}{playbackLabel}</button>}<button className="secondary-button" onClick={() => set({ dialog: 'experiment' })}><Plus size={15} />Draft experiment</button></div></section>
        <div className="context-bar"><div className="scenario-context"><Box size={15} /><select aria-label="Example architecture" value={scenarioId} onChange={event => { setScenario(event.target.value); setEntitySearch('') }}>{(scenarios.some(item => item.live) ? scenarios.filter(item => item.live) : scenarios).map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select><span className="context-separator" /><span className="context-type">{scenario.subtitle}</span></div><div className="environment-context"><span>Environment</span><select aria-label="Selected environment" value={environment.id} onChange={event => focus(event.target.value)}>{workspace.environments.map(env => <option key={env.id} value={env.id}>{env.label}</option>)}</select></div></div>

        <section className="workspace-facts" aria-label="Current investigation facts"><span><Waves size={13} />Signal <strong className={reading?.health === 'degraded' ? 'metric-warning' : ''}>{reading?.latency === undefined ? 'Not collected' : `${new Intl.NumberFormat('en-US').format(reading.latency)} ms`}</strong> <span className="fact-muted">at {targetName}</span></span><span><Sparkles size={13} />Question <strong>{questionLabel}</strong></span><span><GitBranch size={13} />Clones <strong>{workspace.environments.length - 1}</strong> <span className="fact-muted">isolated environments</span></span><span><ShieldCheck size={13} />Safety <strong>{workspace.actions.filter(action => action.environmentId === 'production').length} / 5</strong> actions <span className="fact-muted">{activeActions.some(action => action.environmentId === 'production') ? '· TTL bounded' : '· No active intervention'}</span></span></section>

        {view === 'investigation' ? <>
          <section className="lifecycle-summary" aria-label="Incident lifecycle" data-phase={workspace.lifecycle}>
            <div className="lifecycle-copy" aria-live="polite"><strong>{lifecycleStep.title}</strong><p>{workspace.lifecycle === 'confirming' && workspace.verdict ? 'Recovery held after the production probe was released. The recorded evidence now confirms the cause; clone cleanup is next.' : lifecycleStep.detail}</p></div>
            <ol className="lifecycle-steps">{lifecycleSteps.map((step, index) => <li key={step.id} data-state={index < lifecycleIndex ? 'complete' : index === lifecycleIndex ? 'current' : 'upcoming'} aria-current={index === lifecycleIndex ? 'step' : undefined}><span>{step.label}</span></li>)}</ol>
            <div className="lifecycle-environments">{workspace.environments.filter(env => env.id !== 'production').map(env => <span key={env.id} data-environment={env.id} data-lifecycle={env.lifecycle} data-outcome={env.outcome} data-winner={workspace.winner === env.id || undefined}>{env.label}: {env.outcome ? environmentOutcomeLabel[env.outcome] : environmentLifecycleLabel[env.lifecycle]}</span>)}{workspace.lifecycle === 'complete' && <span>Clones retained for review; evidence kept</span>}</div>
            {workspace.lifecycle === 'complete' && <div className="lifecycle-actions"><button className="text-button" onClick={() => set({ dialog: 'report' })}>Open the incident report <ArrowRight size={13} /></button><button className="text-button" onClick={() => set({ view: 'explanation' })}>Review the explanation <ArrowRight size={13} /></button></div>}
          </section>
          <details className="phase-disclosure"><summary>Detailed timeline <span>{phaseNumber} / {phases.length}</span></summary><nav className="investigation-path" aria-label="Investigation phases">{phases.map((phase, index) => {
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
                <div className="map-status"><span className={`map-mode ${playing ? 'is-playing' : ''}`}><i />{scenario.live ? (playing ? 'REPLAY PLAYING' : 'REPLAY PAUSED') : playing ? 'SIMULATION PLAYING' : cursor === 0 ? 'DEMO READY' : cursor >= scenario.duration ? 'DEMO COMPLETE' : 'REPLAY PAUSED'}</span><span>{scenario.live ? 'Readings are 5 s C1 windows; traffic animation is illustrative' : 'Traffic is illustrative, not individual requests'}</span></div>
                <div className="camera-tools"><button className={follow ? 'active' : ''} aria-pressed={follow} onClick={() => set({ follow: !follow })}><Compass size={13} />{follow ? 'Following key events' : 'Follow key events'}</button><button aria-pressed={reducedMotion} onClick={() => set({ reducedMotion: !reducedMotion })}><MousePointer2 size={12} />{reducedMotion ? 'Reduced motion' : 'Full motion'}</button></div>
              </div>
              <div className="map-environments" aria-label="System layers"><button className={isolatedLayer === null ? 'selected' : ''} aria-pressed={isolatedLayer === null} onClick={() => { focus('production'); set({ isolatedLayer: null }); setEvidenceOpen(false) }}><Layers3 size={13} /><strong>All layers</strong></button>{workspace.environments.map(env => <button data-environment={env.id} data-lifecycle={env.lifecycle} data-outcome={env.outcome} data-winner={workspace.winner === env.id || undefined} className={isolatedLayer === env.id ? 'selected' : ''} aria-label={`Focus ${env.label} layer`} aria-pressed={isolatedLayer === env.id} key={env.id} onClick={() => focus(env.id)}><span style={{ background: env.color }} /><strong>{env.label}</strong><small>{env.level === 0 ? 'Reference system' : env.outcome ? environmentOutcomeLabel[env.outcome] : environmentLifecycleLabel[env.lifecycle]}</small>{env.id !== 'production' && <GitBranch size={12} />}</button>)}</div>
              <Timeline scenario={scenario} />
            </section>
            {showInspector && <Inspector scenario={scenario} workspace={workspace} environment={environment} />}
          </div>
          {showInspector && <div className="below-stage"><section className="panel pulse-chart"><div className="panel-title"><div><span className="overline">THE RESPONSE, IN CONTEXT</span><h3>{selectedNode ?? targetName} <span className="muted">/ {environment.label}</span></h3></div><button className="icon-button" aria-label="Open observability dashboard" onClick={() => set({ view: 'observability' })}><ArrowRight size={15} /></button></div><div className="chart-legend"><i className="latency-line" />Latency · ms<i className="load-line" />Issued load · qps</div><MetricChart scenario={scenario} cursor={cursor} environmentId={environment.id} nodeId={selectedNode ?? scenario.targetId} compact /></section><section className="panel decision-summary"><span className="overline"><Sparkles size={12} />LATEST EVIDENCE</span><h3>{latest?.title}</h3><p>{latest?.detail}</p><button className="text-button" onClick={() => set({ traceTab: 'trace', selectedEvent: latest?.id })}>See the decision trace <ArrowRight size={13} /></button><div className="decision-meta"><span>{latest?.environmentId}</span><span>{timeLabel(latest?.at ?? 0)}</span><span>{scenario.live ? 'Recorded audit event' : 'Scripted example'}</span></div></section></div>}
        </> : <>
          {view === 'explanation' && <Explanation scenario={scenario} workspace={workspace} />}
          {view === 'observability' && <Observability scenario={scenario} environment={environment} />}
          {view === 'experiments' && <ExperimentLab scenario={scenario} workspace={workspace} />}
          {view === 'replay' && <ReplayLibrary scenario={scenario} />}
          {view === 'elastic' && <ElasticLineage />}
          <div className="panel page-timeline"><Timeline scenario={scenario} /></div>
        </>}
        <footer className="page-footer"><span><Mark />Faultline <span>·</span> The model proposes. Measurement decides.</span><span>{view === 'comparison' ? 'Read-only comparison replay' : scenario.live ? 'Real audit log · readings from Elasticsearch' : <>Interactive design prototype <ArrowDownRight size={12} /></>}</span></footer>
      </main>
    </div>
    <Dialogs scenario={scenario} workspace={workspace} />
  </div>
}
