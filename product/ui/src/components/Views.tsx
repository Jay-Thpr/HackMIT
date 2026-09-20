import { ArrowRight, Box, Database, FlaskConical, GitBranch, Layers3, Play, Search, ShieldCheck } from 'lucide-react'
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
  const { cursor, seek, set } = useWorkspace()
  return <div className="lab-view">
    <section className="lab-banner"><div className="lab-banner-icon"><FlaskConical size={29} strokeWidth={1.2} /></div><div><span className="overline">CLONE EXPERIMENTS</span><h2>Test a possible cause.</h2><p>Choose a cause, predict what should happen, and test it in a clone.</p></div><button className="primary-button" onClick={() => set({ dialog: 'experiment' })}>Design an experiment <ArrowRight size={15} /></button></section>
    <div className="section-heading"><h3>Isolated environments</h3><span>{workspace.environments.length - 1} active in this replay</span></div>
    <div className="clone-card-grid">{scenario.hypotheses.map(hypothesis => {
      const creation = scenario.events.find(event => event.kind === 'clone' && event.environment?.hypothesisId === hypothesis.id)
      const environmentId = creation?.environmentId
      const observation = scenario.events.find(event => event.kind === 'observe' && event.environmentId === environmentId)
      const environment = workspace.environments.find(env => env.id === environmentId)
      return <article className="panel lab-clone-card" key={hypothesis.id}><div className="clone-card-top"><span className="clone-icon" style={{ color: hypothesis.color }}><GitBranch size={23} /></span><span className="quiet-badge">{environment ? 'Isolated · simulated' : 'Not active at this time'}</span></div><span className="overline">HYPOTHESIS {hypothesis.id}</span><h3>{hypothesis.title}</h3><p>{hypothesis.description}</p><dl><div><dt>Inherits</dt><dd>Topology, versions, workload</dd></div><div><dt>Does not inherit</dt><dd>Production data or hidden state</dd></div><div><dt>Observation</dt><dd>{environment ? `Through ${timeLabel(cursor)}` : 'No active clone'}</dd></div></dl><button className="secondary-button" disabled={!creation} onClick={() => { if (!creation || !environmentId) return; seek(observation?.at ?? creation.at); set({ view: 'investigation', environmentId, selectedNode: undefined, focusRevision: useWorkspace.getState().focusRevision + 1 }) }}>Explore this reproduction <ArrowRight size={14} /></button></article>
    })}</div>
    <section className="panel lab-safety"><ShieldCheck size={23} /><div><h3>Nothing runs against your infrastructure here.</h3><p>These are simulated experiments. Running a real test requires a connected backend and a verified way to undo the change.</p></div></section>
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
