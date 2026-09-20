import { ArrowRight, Box, Database, GitBranch, Layers3, Play, Search } from 'lucide-react'
import { useState } from 'react'
import { metricLabel, timeLabel, visibleEvents, type Environment, type Scenario, type WorkspaceState } from '../model'
import { useWorkspace } from '../store'
import { MetricChart } from './MetricChart'

export function Observability({ scenario, environment }: { scenario: Scenario; environment: Environment }) {
  const { cursor, selectedNode, inspect, set } = useWorkspace()
  const [search, setSearch] = useState('')
  const nodeId = selectedNode ?? scenario.targetId
  return <div className="observability-view">
    <section className="panel big-chart"><div className="panel-title"><div><span className="overline">LATENCY & LOAD</span><h3>{nodeId} <span className="muted">/ {environment.label}</span></h3></div><span className="chart-legend"><i className="latency-line" />Latency · ms<i className="load-line" />Issued load · qps</span></div><MetricChart scenario={scenario} cursor={cursor} environmentId={environment.id} nodeId={nodeId} /><div className="chart-footnote">Illustrative values through {timeLabel(cursor)}. Blank regions contain no samples; they are not zero. Shaded spans mark interventions.</div></section>
    <section className="panel service-table"><div className="panel-title"><div><span className="overline">SERVICE INVENTORY</span><h3>Services & dependencies</h3></div><label className="search-input"><Search size={14} /><input aria-label="Filter services" value={search} onChange={event => setSearch(event.target.value)} placeholder="Find a service…" /></label></div>
      <div className="table-scroll"><table><thead><tr><th>Entity</th><th>Health</th><th>p99 latency</th><th>Issued load</th><th>Error rate</th><th>Visibility</th><th /></tr></thead><tbody>{scenario.topology.nodes.filter(node => node.label.toLowerCase().includes(search.toLowerCase())).map(node => {
        const reading = environment.nodes[node.id]
        const Icon = node.kind === 'datastore' ? Database : node.kind === 'queue' ? Layers3 : Box
        return <tr key={node.id}><td><span className="table-entity"><Icon size={15} />{node.label}</span></td><td><span className={`health-tag ${reading?.health ?? 'unknown'}`}><i />{reading?.health ?? 'unknown'}</span></td><td className="mono">{metricLabel(reading?.latency, 'ms')}</td><td className="mono">{metricLabel(reading?.qps, 'qps')}</td><td className="mono">{metricLabel(reading?.errorRate, '%')}</td><td>{node.instrumented ? 'Service metrics' : 'Dependency only'}</td><td><button className="icon-button" aria-label={`Inspect ${node.label} metrics`} onClick={() => inspect(node.id, environment.id)}><ArrowRight size={15} /></button></td></tr>
      })}</tbody></table></div>
      {!scenario.topology.nodes.some(node => node.label.toLowerCase().includes(search.toLowerCase())) && <p className="empty-copy">No entities match “{search}”.</p>}
    </section>
    <div className="coverage-grid">{[['Logs & traces', 'Not connected', 'Connect your telemetry source to inspect raw evidence.'], ['Instance inventory', 'Not collected', 'Service nodes are not a claim about container or shard count.'], ['Source provenance', 'Simulated dataset', 'No Elasticsearch query or live observation produced these values.']].map(([title, value, description]) => <section className="panel coverage-card" key={title}><span className="overline">{title}</span><h3>{value}</h3><p>{description}</p></section>)}</div>
    <button className="text-button" onClick={() => set({ view: 'investigation' })}>Return to the spatial investigation <ArrowRight size={14} /></button>
  </div>
}

export function ExperimentLab({ scenario, workspace }: { scenario: Scenario; workspace: WorkspaceState }) {
  const { cursor, seek, focus, set, startDemo } = useWorkspace()
  const events = visibleEvents(scenario, cursor)
  const hypotheses = events.some(event => event.kind === 'reason') ? scenario.hypotheses : []
  const clones = workspace.environments.filter(environment => environment.id !== 'production')
  const lifecycleLabels = { unknown: 'Readiness not recorded', starting: 'Starting', ready: 'Ready', investigating: 'Investigating', destroying: 'Removing' }
  return <div className="lab-view">
    <section className="lab-banner"><div><h2>Compare causes in isolated copies.</h2><p>Clones are isolated test environments built from the system’s topology, versions, and workload. Each investigator tests one possible cause without copying production data or hidden fault state.</p><p className="page-instruction">Inspect a clone to follow its agent and test results in the 3D workspace.</p></div><div className="lab-draft-action"><button className="primary-button" onClick={() => set({ dialog: 'experiment' })}>Draft a test <ArrowRight size={15} /></button><small>Validate a plan only; nothing runs.</small></div></section>
    <div className="section-heading"><h3>Clones in this demo</h3><span>{clones.length} present · {timeLabel(cursor)}</span></div>
    {hypotheses.length === 0 ? <section className="lab-empty"><h3>No clone investigation yet</h3><p>The demo starts healthy. After an incident is detected, Faultline proposes causes, starts clean clones, and tests them before confirming a result and removing the clones.</p><button className="secondary-button" onClick={() => startDemo(scenario.id)}><Play size={14} />Watch incident demo</button></section> : <div className="clone-card-grid">{hypotheses.map(hypothesis => {
      const creation = events.find(event => event.kind === 'clone' && event.environment?.hypothesisId === hypothesis.id)
      const environmentId = creation?.environmentId
      const environment = clones.find(env => env.id === environmentId)
      const archive = events.find(event => event.kind === 'archive' && event.environmentId === environmentId)
      const latest = environmentId ? events.filter(event => event.environmentId === environmentId).at(-1) : undefined
      const status = environment ? lifecycleLabels[environment.lifecycle] : archive ? 'Archived' : 'Waiting for clone'
      const statusId = `clone-status-${hypothesis.id}`
      return <article className="panel lab-clone-card" key={hypothesis.id}>
        <div className="clone-card-top"><span className="clone-identity"><GitBranch size={17} />{creation?.environment?.label ?? `Hypothesis ${hypothesis.id}`}</span><span className="clone-lifecycle" data-state={environment?.lifecycle ?? (archive ? 'archived' : 'waiting')}>{status}</span></div>
        <h3>{hypothesis.title}</h3><p>{hypothesis.description}</p>
        <dl><div><dt>Expected response</dt><dd>{hypothesis.prediction}</dd></div><div><dt>Latest step</dt><dd id={statusId}>{latest?.title ?? 'Continue the timeline to see this clone start.'}</dd></div></dl>
        <button className="secondary-button" disabled={!environment && !archive} aria-describedby={statusId} onClick={() => {
          if (!creation || !environmentId) return
          if (archive) seek(creation.at)
          focus(environmentId)
          set({ view: 'investigation', playing: Boolean(archive), follow: Boolean(archive) })
        }}>{archive ? <><Play size={14} />Replay this experiment</> : environment ? <>Inspect clone <ArrowRight size={14} /></> : 'Waiting for clone startup'}</button>
      </article>
    })}</div>}
    <p className="page-source-note">Simulated clone lifecycle and test results. No infrastructure is connected.</p>
  </div>
}

export function ReplayLibrary({ scenario }: { scenario: Scenario }) {
  const { cursor, set, startDemo, scenarios } = useWorkspace()
  const events = visibleEvents(scenario, cursor)
  const verdict = events.filter(event => event.kind === 'verdict' && event.environmentId === 'production').at(-1)
  const detected = events.some(event => event.kind === 'detect')
  const archivedCount = events.filter(event => event.kind === 'archive').length
  return <div className="replay-view">
    <section className="panel replay-hero"><h2>Review the incident from the beginning.</h2><p>Choose a demo to watch healthy operation, an incident, clone startup, investigation, confirmation, and cleanup. Playback opens the 3D workspace with the camera following each step.</p><span className="page-source-note">Scripted examples, not a history of saved incidents.</span></section>
    <section className="panel report-preview"><div className="report-heading"><div><span className="overline">SELECTED DEMO · {scenario.incident}</span><h2>{scenario.name}</h2></div><span className="quiet-badge">Report at {timeLabel(cursor)}</span></div><h3>{verdict?.title ?? (detected ? 'Investigation in progress' : 'Healthy reference, before the incident')}</h3><p>{verdict?.detail ?? (detected ? 'Evidence is still being gathered. A conclusion appears only when the confirmation event is reached.' : 'No incident has been detected at this point in the demo. Replay it to follow the investigation.')}</p><div className="report-facts"><span>Timeline<strong>{timeLabel(cursor)} / {timeLabel(scenario.duration)}</strong></span><span>Simulated production probes<strong>{events.filter(event => event.kind === 'action' && event.environmentId === 'production').length}</strong></span><span>Clones removed<strong>{archivedCount}</strong></span></div><button className="text-button" onClick={() => set({ view: 'investigation', playing: false })}>Inspect this moment in 3D <ArrowRight size={14} /></button></section>
    <section className="replay-demos" aria-labelledby="available-demos-title"><div className="section-heading"><h3 id="available-demos-title">Available demos</h3><span>{scenarios.length} examples</span></div>{scenarios.map(demo => <article className="replay-row" key={demo.id} data-selected={demo.id === scenario.id}><div><div className="demo-identity"><h3>{demo.name}</h3>{demo.id === scenario.id && <span className="quiet-badge">Selected</span>}</div><p>{demo.incident} · {demo.incidentTitle}</p><small>{demo.subtitle} · {demo.duration}s simulated timeline</small></div><button className="secondary-button" aria-label={`Replay this demo: ${demo.name}`} onClick={() => startDemo(demo.id)}><Play size={14} />Replay this demo</button></article>)}</section>
  </div>
}
