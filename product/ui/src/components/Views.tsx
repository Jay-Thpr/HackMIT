import { ArrowRight, Box, ChevronLeft, ChevronRight, Database, GitBranch, Layers3, Play, Search } from 'lucide-react'
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
  const [query, setQuery] = useState('')
  type ReplayFilter = 'all' | 'example' | 'ready' | 'mitigated' | 'escalated' | 'in-progress'
  const [outcome, setOutcome] = useState<ReplayFilter>('all')
  const shown = visibleEvents(scenario, cursor)
  const verdict = shown.filter(event => event.kind === 'verdict' && event.environmentId === 'production').at(-1)
  const finalVerdict = scenario.events.filter(event => event.kind === 'verdict' && event.environmentId === 'production').at(-1)
  const live = scenarios.filter(item => item.live)
  const examples = scenarios.filter(item => !item.live)
  const report = scenario.report
  const outcomeOf = (item: Scenario): Exclude<ReplayFilter, 'all'> => {
    if (!item.live) return 'example'
    const text = item.report?.outcome ?? ''
    if (!item.complete) return 'in-progress'
    if (text.startsWith('incident report ready')) return 'ready'
    if (text.startsWith('incident mitigated')) return 'mitigated'
    return 'escalated'
  }
  const outcomeLabel = { example: 'Example', ready: 'Ready', mitigated: 'Mitigated', escalated: 'Escalated', 'in-progress': 'In progress' }
  const matches = (item: Scenario) => {
    if (outcome !== 'all' && outcomeOf(item) !== outcome) return false
    const needle = query.trim().toLowerCase()
    if (!needle) return true
    return [item.id, item.name, item.incident, item.incidentTitle, item.report?.diagnosis ?? '', item.report?.outcome ?? '', item.report?.patch ?? ''].some(field => field.toLowerCase().includes(needle))
  }
  const filtered = scenarios.filter(matches)
  const counts = { all: scenarios.length, example: 0, ready: 0, mitigated: 0, escalated: 0, 'in-progress': 0 } as Record<ReplayFilter, number>
  for (const item of scenarios) counts[outcomeOf(item)]++
  const position = live.findIndex(item => item.id === scenario.id)
  const neighbour = (step: number) => live[position + step]
  const review = (id: string) => {
    // Review = the whole recorded run: select it and put the cursor at its end so the report shows
    const target = scenarios.find(item => item.id === id)
    if (!target) return
    if (id !== scenario.id) setScenario(id)
    useWorkspace.getState().seek(target.duration)
  }
  const playFromStart = (id: string) => { if (id !== scenario.id) setScenario(id); useWorkspace.getState().seek(0); set({ view: 'investigation', playing: true }) }
  const patchLabel = report?.patch
    ? `${report.patchProvider ?? 'patch'}${report.patchRevision ? ` rev ${report.patchRevision}` : ''} · verification ${report.verification ?? '—'} · canary ${report.canary ?? '—'}`
    : scenario.live ? 'No patch recorded' : 'Not connected'
  const headline = (item: Scenario) => item.report?.diagnosis ? `${item.incidentTitle} → ${item.report.diagnosis}${item.report.confirmed ? ' confirmed' : ' not confirmed'}` : item.incidentTitle
  const started = (item: Scenario) => item.report?.startedAt ? new Date(item.report.startedAt).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : ''
  return <div className="replay-view">
    {scenario.live ? <section className="panel replay-selected" aria-label="Selected incident">
      <div className="replay-selected-heading">
        <div><span className="overline">SELECTED INCIDENT · {scenario.id}{position >= 0 ? ` · ${position + 1} of ${live.length}` : ''}</span><h2>{headline(scenario)}</h2><p>{report?.outcome ?? 'in progress'} · started {started(scenario)} · {timeLabel(scenario.duration)} · {scenario.events.length} audited steps</p></div>
        <div className="replay-step"><button className="secondary-button" disabled={!neighbour(-1)} onClick={() => neighbour(-1) && review(neighbour(-1)!.id)} aria-label="Newer incident"><ChevronLeft size={14} />Newer</button><button className="secondary-button" disabled={!neighbour(1)} onClick={() => neighbour(1) && review(neighbour(1)!.id)} aria-label="Older incident">Older<ChevronRight size={14} /></button></div>
      </div>
      <div className="report-preview report-preview-inline">
        <span className="overline">REPORT AT THE SELECTED TIME · {timeLabel(cursor)}</span>
        <h3>{verdict?.title ?? (finalVerdict ? 'The verdict is later on the timeline.' : !scenario.complete ? 'Investigation in progress.' : 'Investigation was not confirmed.')}</h3>
        <p>{verdict?.detail ?? (finalVerdict ? `Measurement reached “${finalVerdict.title}” at ${timeLabel(finalVerdict.at)}. Move the timeline forward, or review the full report.` : 'The report will only show conclusions whose evidence exists at the selected point on the timeline.')}</p>
        {verdict?.result && <p className="report-evidence">{verdict.result}</p>}
        <div className="report-facts">
          <span>Source<strong>Audit log · {scenario.id}</strong></span>
          <span>Production actions<strong>{shown.filter(event => event.kind === 'action' && event.environmentId === 'production').length}{report ? ` / ${report.productionActions}` : ''}</strong></span>
          <span>Patch / canary<strong>{patchLabel}</strong></span>
          <span>{report?.mitigationHeld ? 'Mitigation held' : 'Benchmark accuracy'}<strong>{report?.mitigationHeld ?? 'Not measured'}</strong></span>
        </div>
        <div className="replay-actions">
          {!verdict && finalVerdict && <button className="primary-button" onClick={() => seek(scenario.duration)}>Review the full report<ArrowRight size={14} /></button>}
          {verdict && <button className="primary-button" onClick={() => set({ dialog: 'report' })}>Open the incident report<ArrowRight size={14} /></button>}
          <button className="secondary-button" onClick={() => playFromStart(scenario.id)}><Play size={14} />Play from the start</button>
          <a className="secondary-button" href={`/api/incidents/${encodeURIComponent(scenario.id)}/evidence.json`} download>Export evidence</a>
        </div>
      </div>
    </section> : <section className="panel replay-hero"><span className="overline">INCIDENT MEMORY</span><h2>Review an investigation.</h2><p>Replay the incident to see what the agent changed, what happened next, and how it reached a conclusion. The report reveals only conclusions whose evidence exists at the selected point on the timeline.</p><span className="quiet-badge">{examples.length} example investigation{examples.length === 1 ? '' : 's'}{live.length ? ` · ${live.length} recorded incident${live.length === 1 ? '' : 's'}` : ' · no recorded incidents loaded'}</span></section>}

    {scenarios.length > 0 && <section className="replay-browse" aria-label="Available investigations">
      <div className="replay-browse-controls">
        <label className="search-input"><Search size={14} /><input aria-label="Find an incident" placeholder="Find by id, diagnosis, outcome or patch…" value={query} onChange={event => setQuery(event.target.value)} /></label>
        <div className="replay-filters" role="tablist" aria-label="Filter investigations">{(['all', 'example', 'ready', 'mitigated', 'escalated', 'in-progress'] as const).map(key => <button key={key} role="tab" aria-selected={outcome === key} className={outcome === key ? 'selected' : ''} onClick={() => setOutcome(key)}>{key === 'all' ? 'All' : outcomeLabel[key]}<small>{counts[key]}</small></button>)}</div>
      </div>
      <ol className="replay-list">{filtered.map(item => <li key={item.id} className={item.id === scenario.id ? 'is-selected' : ''} aria-label={`${item.live ? 'Incident' : 'Example investigation'} ${item.name} ${item.id}`} aria-current={item.id === scenario.id ? 'true' : undefined}>
        <button className="replay-list-main" onClick={() => review(item.id)}>
          <span className="replay-list-id">{item.live ? item.id : item.incident}</span>
          <span className="replay-list-title">{item.live ? headline(item) : `${item.name} · ${item.incidentTitle}`}</span>
          <span className="replay-list-meta"><span className={`replay-outcome ${outcomeOf(item)}`}>{outcomeLabel[outcomeOf(item)]}</span>{item.live ? `${started(item)} · ${timeLabel(item.duration)} · ${item.report?.productionActions ?? 0} actions` : `${timeLabel(item.duration)} · ${item.events.length} scripted steps`}</span>
        </button>
        <button className="icon-button" aria-label={`Play ${item.id} from the start`} title="Play from the start" onClick={() => playFromStart(item.id)}><Play size={14} /></button>
      </li>)}</ol>
      {filtered.length === 0 && <p className="empty-copy">No incidents match “{query}”{outcome !== 'all' ? ` with outcome ${outcomeLabel[outcome]}` : ''}.</p>}
    </section>}

    {!scenario.live && <section className="panel replay-row"><div className="replay-icon"><Layers3 size={23} /></div><div><span className="overline">ILLUSTRATIVE REPLAY · {scenario.incident}</span><h3>{scenario.incidentTitle}</h3><p>{scenario.name} · {scenario.duration}s simulated timeline · {scenario.events.length} scripted steps</p></div><button className="secondary-button" onClick={() => { seek(0); set({ view: 'investigation', playing: true }) }}><Play size={14} />Play from the start</button></section>}
    {!scenario.live && <section className="panel report-preview"><span className="overline">REPORT AT THE SELECTED TIME · {timeLabel(cursor)}</span>
      <h3>{verdict?.title ?? (finalVerdict ? 'The verdict is later on the timeline.' : 'Investigation is not yet confirmed.')}</h3>
      <p>{verdict?.detail ?? (finalVerdict ? `Measurement reached “${finalVerdict.title}” at ${timeLabel(finalVerdict.at)}. Move the timeline forward, or review the full report.` : 'The report will only show conclusions whose evidence exists at the selected point on the timeline.')}</p>
      {!verdict && finalVerdict && <button className="secondary-button" onClick={() => seek(scenario.duration)}>Review the full report<ArrowRight size={14} /></button>}
      {verdict?.result && <p className="report-evidence">{verdict.result}</p>}
      <div className="report-facts">
        <span>Source<strong>Scripted example</strong></span>
        <span>Production actions<strong>{shown.filter(event => event.kind === 'action' && event.environmentId === 'production').length}</strong></span>
        <span>Patch / canary<strong>Not connected</strong></span>
        <span>Benchmark accuracy<strong>Not measured</strong></span>
      </div>
    </section>}
  </div>
}

export function ElasticLineage() {
  const { set } = useWorkspace()
  const [surface, setSurface] = useState<'timeline' | 'clone' | 'memory'>('timeline')
  const stages = [
    ['01', 'OpenTelemetry', 'Traces, metrics, and logs enter through one standard collector.'],
    ['02', 'Elastic Cloud', 'Raw signals remain inspectable beside the structured incident record.'],
    ['03', 'Evidence', 'C1 fingerprints and C4 audit events keep each decision reproducible.'],
    ['04', 'Retrieve', 'Bounded ES|QL tools and Jina memory surface only scoped context.'],
    ['05', 'Decide', 'OpenAI explains the evidence; the noise-model judge owns the verdict.'],
  ]
  const querySurfaces = {
    timeline: {
      label: 'Incident timeline',
      tool: 'faultline.incident_timeline',
      purpose: 'Shows the observed sequence without asking the agent to sift through every log line.',
      parameters: ['incident_id: INC-042', 'environment: production', 'start / end: bounded UTC window'],
      query: `FROM faultline-fingerprints\n| WHERE incident_id == ?incident_id\n  AND environment == ?environment\n  AND window_start >= TO_DATETIME(?start)\n  AND window_start < TO_DATETIME(?end)\n| KEEP window_start, services.orders.qps,\n       services.orders.retry_ratio, db.query_p99_ms\n| SORT window_start ASC\n| LIMIT 200`,
    },
    clone: {
      label: 'Clone comparison',
      tool: 'faultline.clone_vs_production',
      purpose: 'Checks whether an isolated experiment resembles production before its result is trusted.',
      parameters: ['incident_id: INC-042', 'clone_id: clone-db', 'same bounded UTC window'],
      query: `FROM faultline-fingerprints\n| WHERE incident_id == ?incident_id\n  AND (environment == "production"\n       OR clone_id == ?clone_id)\n| STATS windows = COUNT(*),\n        retry_mean = AVG(services.orders.retry_ratio),\n        db_p99_mean = AVG(db.query_p99_ms)\n  BY environment, clone_id\n| LIMIT 2`,
    },
    memory: {
      label: 'Semantic memory',
      tool: 'faultline.semantic_incident_memory',
      purpose: 'Finds a related operator report when its wording differs from the current incident.',
      parameters: ['observed query text only', 'environment: production', 'bounded historical window'],
      query: `FROM faultline-incident-memory METADATA _score\n| WHERE environment == ?environment\n  AND incident_id != ?incident_id\n  AND created_at >= TO_DATETIME(?start)\n  AND created_at < TO_DATETIME(?end)\n  AND MATCH(content, ?query_text)\n| KEEP incident_id, diagnosis, content, _score\n| SORT _score DESC\n| LIMIT 10`,
    },
  } as const
  const selectedSurface = querySurfaces[surface]
  return <div className="elastic-lineage-view">
    <section className="elastic-intro">
      <div className="elastic-attribution"><img src="https://www.elastic.co/favicon.ico" alt="Elastic" /><span>Built with Elastic</span></div>
      <span className="overline">THE EVIDENCE LINEAGE</span>
      <h2>From noisy telemetry to a decision you can inspect.</h2>
      <p>Elastic is the evidence layer: it keeps the raw signal, the incident record, and the bounded retrieval path connected. Faultline’s judge still decides from measured change.</p>
    </section>
    <section className="elastic-pipeline" aria-label="Faultline and Elastic evidence pipeline">
      {stages.map(([number, title, detail], index) => <div className="elastic-stage" key={title}>
        <span className="elastic-stage-number">{number}</span>
        <div><h3>{title}</h3><p>{detail}</p></div>
        {index < stages.length - 1 && <span className="elastic-connector" aria-hidden="true" />}
      </div>)}
    </section>
    <section className="elastic-guardrail"><span>WHY THIS MATTERS</span><p>Historical similarity can add context. It cannot name a cause, select a production action, or override the measured confirmation test.</p></section>
    <section className="query-surface" aria-labelledby="query-surface-title">
      <div className="query-surface-heading"><div><span className="overline">INSPECT THE RETRIEVAL</span><h3 id="query-surface-title">The agent sees reviewed tools, not open-ended search.</h3></div><span className="query-readonly">Read-only · bounded</span></div>
      <div className="query-tabs" role="tablist" aria-label="Elastic tool examples">{(Object.keys(querySurfaces) as (keyof typeof querySurfaces)[]).map(key => <button key={key} role="tab" aria-selected={surface === key} onClick={() => setSurface(key)}>{querySurfaces[key].label}</button>)}</div>
      <div className="query-detail">
        <div className="tool-call-card"><span className="overline">AGENT BUILDER TOOL CALL</span><strong>{selectedSurface.tool}</strong><p>{selectedSurface.purpose}</p><div className="tool-parameters"><span>Required inputs</span>{selectedSurface.parameters.map(parameter => <code key={parameter}>{parameter}</code>)}</div><small>Only these named values can change. The query structure is fixed in Faultline.</small></div>
        <div className="query-code"><div><span>ES|QL</span><span>Reviewed query template</span></div><pre><code>{selectedSurface.query}</code></pre></div>
      </div>
      <p className="query-caption">The full system also has fixed tools for audit context and similar incidents. Semantic memory is context only; the measurement judge still makes the verdict.</p>
    </section>
    <div className="elastic-actions"><button className="primary-button" onClick={() => set({ view: 'investigation' })}>See the investigation <ArrowRight size={14} /></button><button className="text-button" onClick={() => set({ view: 'explanation' })}>Read the decision trace <ArrowRight size={14} /></button></div>
    <p className="page-source-note">Design preview. This page explains the intended evidence flow; it does not query Elastic Cloud.</p>
  </div>
}
