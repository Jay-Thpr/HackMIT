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
  const lifecycleLabels = { unknown: 'Readiness not recorded', starting: 'Starting', ready: 'Ready', investigating: 'Investigating', destroying: 'Removing', archived: 'Archived · retained for review' }
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
  const { cursor, seek, set, scenarios, setScenario } = useWorkspace()
  const shown = visibleEvents(scenario, cursor)
  const verdict = shown.filter(event => event.kind === 'verdict' && event.environmentId === 'production').at(-1)
  const finalVerdict = scenario.events.filter(event => event.kind === 'verdict' && event.environmentId === 'production').at(-1)
  const live = scenarios.filter(item => item.live)
  const report = scenario.report
  const review = (id: string) => {
    // Review = the whole recorded run: select it and put the cursor at its end so the report shows
    const target = scenarios.find(item => item.id === id)
    if (!target) return
    if (id !== scenario.id) setScenario(id)
    useWorkspace.getState().seek(target.duration)
  }
  const patchLabel = report?.patch
    ? `${report.patchProvider ?? 'patch'}${report.patchRevision ? ` rev ${report.patchRevision}` : ''} · verification ${report.verification ?? '—'} · canary ${report.canary ?? '—'}`
    : scenario.live ? 'No patch recorded' : 'Not connected'
  return <div className="replay-view">
    <section className="panel replay-hero"><span className="overline">INCIDENT MEMORY</span><h2>Review an investigation.</h2><p>Replay the incident to see what the agent changed, what happened next, and how it reached a conclusion. The report reveals only conclusions whose evidence exists at the selected point on the timeline.</p><span className="quiet-badge">{live.length ? `${live.length} recorded incident${live.length === 1 ? '' : 's'} from the audit log` : 'Design preview · no recorded incidents loaded'}</span></section>
    {live.map(item => <section key={item.id} className={`panel replay-row ${item.id === scenario.id ? 'is-selected' : ''}`} aria-label={`Incident ${item.id}`}>
      <div className="replay-icon"><Layers3 size={23} /></div>
      <div><span className="overline">{item.complete ? 'RECORDED INCIDENT' : 'LIVE INCIDENT · IN PROGRESS'} · {item.id}</span><h3>{item.report?.diagnosis ? `${item.incidentTitle} → ${item.report.diagnosis}${item.report.confirmed ? ' confirmed' : ' not confirmed'}` : item.incidentTitle}</h3><p>{item.report?.outcome ?? 'in progress'} · {timeLabel(item.duration)} · {item.events.length} audited steps · {item.report?.productionActions ?? 0} production actions</p></div>
      <div className="replay-actions"><button className="secondary-button" onClick={() => review(item.id)}>Review<ArrowRight size={14} /></button><button className="secondary-button" onClick={() => { if (item.id !== scenario.id) setScenario(item.id); useWorkspace.getState().seek(0); set({ view: 'investigation', playing: true }) }}><Play size={14} />Play from the start</button></div>
    </section>)}
    {!scenario.live && <section className="panel replay-row"><div className="replay-icon"><Layers3 size={23} /></div><div><span className="overline">ILLUSTRATIVE REPLAY · {scenario.incident}</span><h3>{scenario.incidentTitle}</h3><p>{scenario.name} · {scenario.duration}s simulated timeline · {scenario.events.length} scripted steps</p></div><button className="secondary-button" onClick={() => { seek(0); set({ view: 'investigation', playing: true }) }}><Play size={14} />Play from the start</button></section>}
    <section className="panel report-preview"><span className="overline">REPORT AT THE SELECTED TIME · {timeLabel(cursor)}</span>
      <h3>{verdict?.title ?? (finalVerdict ? 'The verdict is later on the timeline.' : scenario.live && !scenario.complete ? 'Investigation in progress.' : 'Investigation is not yet confirmed.')}</h3>
      <p>{verdict?.detail ?? (finalVerdict ? `Measurement reached “${finalVerdict.title}” at ${timeLabel(finalVerdict.at)}. Move the timeline forward, or review the full report.` : 'The report will only show conclusions whose evidence exists at the selected point on the timeline.')}</p>
      {!verdict && finalVerdict && <button className="secondary-button" onClick={() => seek(scenario.duration)}>Review the full report<ArrowRight size={14} /></button>}
      {verdict?.result && <p className="report-evidence">{verdict.result}</p>}
      <div className="report-facts">
        <span>Source<strong>{scenario.live ? `Audit log · ${scenario.id}` : 'Scripted example'}</strong></span>
        <span>Production actions<strong>{shown.filter(event => event.kind === 'action' && event.environmentId === 'production').length}{report ? ` / ${report.productionActions}` : ''}</strong></span>
        <span>Patch / canary<strong>{patchLabel}</strong></span>
        <span>{report?.mitigationHeld ? 'Mitigation held' : 'Benchmark accuracy'}<strong>{report?.mitigationHeld ?? 'Not measured'}</strong></span>
      </div>
    </section>
  </div>
}
