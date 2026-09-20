import { ArrowDownRight, ArrowRight, Check, ChevronDown, ChevronRight, Clock3, FlaskConical, ScanLine, Terminal, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { diagnosisSummary, isConfirmedUndo, isConfirmedVerdict, metricLabel, resourceUnit, timeLabel, visibleEvents, type Environment, type Scenario, type WorkspaceEvent, type WorkspaceState } from '../model'
import { agentActivity } from '../agent-activity'
import { TestCases } from './TestCases'
import { suiteChecks } from '../suite'
import { deriveNodeRecovery } from '../recovery'
import { useWorkspace } from '../store'

const actorLabel: Record<WorkspaceEvent['actor'], string> = { model: 'Model proposal', math: 'Measured evaluation', adapter: 'Tool execution', 'investigator-a': 'Investigator A', 'investigator-b': 'Investigator B', orchestrator: 'Orchestrator', elastic: 'Read-only telemetry responder' }

function TraceStep({ event, selected, onSelect, live }: { event: WorkspaceEvent; selected: boolean; onSelect: () => void; live?: boolean }) {
  return <div className={`trace-step ${selected ? 'expanded' : ''}`}>
    <button className="trace-step-heading" onClick={onSelect} aria-expanded={selected}>
      <span className={`trace-glyph ${event.actor === 'math' ? 'math' : ''}`}>{event.kind === 'observe' ? <ScanLine size={13} /> : event.kind === 'action' ? <Terminal size={13} /> : isConfirmedVerdict(event) ? <Check size={13} /> : <FlaskConical size={13} />}</span>
      <span className="trace-copy"><span>{event.title}</span><small>{event.environmentId} · {timeLabel(event.at)}</small></span>
      {selected ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
    </button>
    {selected && <div className="trace-step-detail">
      <span className="overline">{actorLabel[event.actor]}</span>
      <p>{event.detail}</p>
      {event.tool && <code className="tool-name">{event.tool}({event.args ? Object.entries(event.args).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(', ') : ''})</code>}
      {event.prediction && <div className="trace-prediction"><b>Predicted</b><p>{event.prediction}</p></div>}
      {event.result && <div className="trace-result"><b>Observed / returned</b><p>{event.result}</p></div>}
      {event.causeId && <small className="causal-link"><ArrowDownRight size={12} /> Follows evidence {event.causeId}</small>}
      <span className="data-caption">{live ? `Recorded audit event · ${event.actor === 'model' ? 'model output' : event.actor === 'math' ? 'measured' : 'orchestrator record'}` : 'Scripted example · not a live model call'}</span>
    </div>}
  </div>
}

export function Inspector({ scenario, workspace, environment }: { scenario: Scenario; workspace: WorkspaceState; environment: Environment }) {
  const { cursor, selectedAgent, selectedNode, selectedSuiteCheck, selectedEvent, traceTab, inspect, set } = useWorkspace()
  const events = visibleEvents(scenario, cursor)
  const [filter, setFilter] = useState('all')
  useEffect(() => { setFilter('all') }, [scenario.id])
  const node = scenario.topology.nodes.find(item => item.id === selectedNode)
  const suiteCheck = selectedSuiteCheck && suiteChecks(scenario, selectedSuiteCheck.environmentId, cursor).find(check => check.id === selectedSuiteCheck.checkId)
  const agent = selectedAgent ? agentActivity(scenario, selectedAgent, cursor) : undefined
  const recovery = node ? deriveNodeRecovery(scenario, environment, node.id, cursor) : 'unknown'
  const reading = node && environment.nodes[node.id]
  const filtered = events.filter(event => (selectedNode ? event.environmentId === environment.id : selectedSuiteCheck ? event.environmentId === selectedSuiteCheck.environmentId : filter === 'all' || event.environmentId === filter) && (!selectedNode || event.targetId === selectedNode))
  const createdClones = events.filter(event => event.kind === 'clone')
  // The read-only responder's own position at this cursor, so both walkthroughs are visible
  // in one panel rather than needing a second view.
  const observerConclusion = events.filter(event => event.actor === 'elastic' && (event.kind === 'reason' || event.kind === 'verdict')).at(-1)
  const observerRead = events.filter(event => event.actor === 'elastic' && event.kind === 'observe').at(-1)
  const observerActions = workspace.actions.filter(action => action.environmentId === workspace.observer?.environmentId).length
  const latest = filtered.at(-1)
  const latestReading = events.filter(event => event.environmentId === environment.id && node && event.readings?.[node.id]).at(-1)
  const hadIssue = Boolean(node && events.some(event => event.environmentId === environment.id && event.readings?.[node.id]?.health === 'degraded'))
  const activeActions = workspace.actions.filter(action => action.environmentId === environment.id && action.targetId === selectedNode && action.status !== 'reverted')
  return <aside className="inspector panel">
    <div className="panel-title inspector-heading"><span className="overline">{agent ? 'AGENT ACTIVITY' : node ? 'ENTITY INSPECTOR' : 'INVESTIGATION'}</span><span className="quiet-badge">{scenario.incident}</span></div>
    {node && <div className="selected-entity">
      <div><strong>{agent?.name ?? node.label}</strong><button className="icon-button" aria-label="Close entity inspector" onClick={() => set({ selectedNode: undefined, selectedAgent: undefined })}><X size={15} /></button></div>
      <p>{agent ? `${environment.label} · Working on ${node.label}` : `${environment.label} · ${node.kind} · ${node.instrumented ? 'Instrumented' : 'Observed dependency'}`}</p>
      {!agent && <><div className="entity-metrics"><span>p99 latency<b>{metricLabel(reading?.latency, 'ms')}</b></span><span>Issued load<b>{metricLabel(reading?.qps, 'qps')}</b></span>
        {Object.entries(reading?.resourceMetrics ?? {}).map(([name, value]) => <span key={name}>{name}<b>{metricLabel(value, resourceUnit(name))}</b></span>)}</div>
      <div className="entity-health"><i className={`health-dot ${reading?.health ?? 'unknown'}`} />{reading?.health ?? 'unknown'}<span>Instances: {node.instances ?? 'not collected'}</span></div>{node.tenants?.length ? <div className="entity-tenants"><b>Tenants served</b><ul>{node.tenants.map(tenant => <li key={tenant}><code>{tenant}</code></li>)}</ul></div> : null}</>}
    </div>}

    {suiteCheck && <div className="selected-entity"><div><strong>{environment.label} · Test suite</strong><button className="icon-button" aria-label="Close test inspector" onClick={() => set({ selectedSuiteCheck: undefined })}><X size={15} /></button></div><p>{scenario.live ? 'Recorded test suite' : 'Simulated tests'}</p></div>}
    <div className="tab-bar" role="tablist" aria-label="Investigation details">
      <button role="tab" aria-selected={traceTab === 'evidence'} onClick={() => set({ traceTab: 'evidence' })}>Evidence</button>
      <button role="tab" aria-selected={traceTab === 'trace'} onClick={() => set({ traceTab: 'trace' })}>Decision trace <span>{events.length}</span></button>
    </div>
    <div className="inspector-body">
      {agent && <section className="agent-brief" aria-label="Agent activity"><span className="overline">{agent.phase} · {agent.targetId}</span><h3>{agent.event?.title ?? 'Waiting for evidence'}</h3><dl><div><dt>What it is doing</dt><dd>{agent.event?.detail}</dd></div><div><dt>Why this step</dt><dd>{agent.why}</dd></div><div><dt>What comes next</dt><dd>{agent.waiting}</dd></div></dl><span className="data-caption">{scenario.live ? 'Recorded audit trail' : 'Recorded simulation'} · {timeLabel(agent.event?.at ?? cursor)}</span></section>}

      {suiteCheck && <section className="suite-detail" aria-label="Test result"><span className="overline">{suiteCheck.state}</span><h3>{suiteCheck.label}</h3><p>{suiteCheck.description}</p><TestCases group={suiteCheck} />{suiteCheck.event?.testResult ? <><div className="suite-assertion"><b>Expected</b><p>{suiteCheck.event.testResult.expected}</p></div><div className="suite-result"><b>Observed · {timeLabel(suiteCheck.event.at)}</b><p>{suiteCheck.event.testResult.observed}</p></div></> : <p>{suiteCheck.state === 'running' ? 'Running — no result yet.' : 'Waiting to run.'}</p>}</section>}

      {node && <section className={`issue-summary ${reading?.health === 'degraded' ? 'is-degraded' : recovery === 'recovering' ? 'is-recovering' : ''}`} aria-label="Issue details"><span>System status</span><h3>{reading?.health === 'degraded' ? 'Elevated latency and errors' : reading?.health === 'unknown' || !reading ? 'No measurements available' : recovery === 'recovering' ? 'Recovering · confirmation pending' : hadIssue ? 'Recovery confirmed' : 'Operating normally'}</h3><p>{reading?.health === 'degraded' ? 'Latency and errors are up. The agent is checking what is keeping the system overloaded.' : latestReading?.detail ?? 'No issues were recorded for this system at this point in the replay.'}</p><dl className="issue-facts"><div><dt>Errors</dt><dd>{metricLabel(reading?.errorRate, '%')}</dd></div><div><dt>Baseline latency</dt><dd>{metricLabel(scenario.baseline[node.id]?.latency, 'ms')}</dd></div>{activeActions.map(action => <div key={action.id}><dt>{action.label}</dt><dd>{action.status === 'awaiting-reversion' ? 'Awaiting confirmed reversion' : `${action.status === 'release-failed' ? 'Release failed · ' : ''}${Math.max(0, Math.ceil(action.start + action.ttl - cursor))}s TTL`}</dd></div>)}</dl></section>}
    {node && !agent && <div className="node-agent-activity"><span>Agent activity here</span><strong>{latest?.title ?? 'No activity yet'}</strong><p>{latest?.detail ?? 'The agent has not worked on this system yet.'}</p>{latest && <small>{actorLabel[latest.actor]} · {timeLabel(latest.at)} · {environment.label}</small>}</div>}
      {traceTab === 'evidence' ? <>
        {workspace.observer && <article className="observer-card" aria-label="Read-only responder conclusion" data-abstained={workspace.observer.abstained || undefined}>
          <div className="observer-top"><span className="overline">READ-ONLY RESPONDER</span><span className="observer-state">{workspace.observer.abstained ? 'Declined to name a cause' : observerConclusion ? 'Concluded' : 'Reading telemetry'}</span></div>
          <h3>{observerConclusion?.title ?? observerRead?.title ?? 'Reading the recorded windows'}</h3>
          {observerConclusion?.detail && <p>{observerConclusion.detail}</p>}
          {!workspace.observer.abstained && workspace.observer.diagnosis && <div className="observer-diagnosis"><b>Diagnosis</b><code>{workspace.observer.diagnosis}</code></div>}
          {observerConclusion?.hypotheses?.length ? <div className="observer-hypotheses"><b>Explanations it kept</b><ul>{observerConclusion.hypotheses.map(item => <li key={item}><code>{item}</code></li>)}</ul></div> : null}
          {observerConclusion?.evidence?.length ? <div className="observer-evidence"><b>Evidence · metric keys</b><ul>{observerConclusion.evidence.map(key => <li key={key}><code>{key}</code></li>)}</ul></div> : null}
          {observerConclusion?.recommendation && <div className="observer-recommendation"><b>Recommends</b><p>{observerConclusion.recommendation}</p></div>}
          <p className="observer-footnote">{observerActions} actions applied. This responder reads telemetry; it does not test the system.</p>
        </article>}
        <div className="evidence-intro"><span className="overline">{cursor < 18 ? 'OBSERVATION' : 'WORKING HYPOTHESES'}</span><p>{cursor < 12 ? 'First, record how the system behaves when it is healthy.' : workspace.verdict ? diagnosisSummary(scenario, workspace) : 'Possible causes still need to be tested against the measurements.'}</p></div>
        {cursor >= 18 && scenario.hypotheses.map(hypothesis => {
          const clone = createdClones.find(event => event.environment?.hypothesisId === hypothesis.id)
          const cloned = Boolean(clone)
          const observed = Boolean(clone && events.some(event => (event.testResult?.checkId === 'reproduction' || event.testResult?.checkId === 'reproduce') && event.testResult.passed && event.environmentId === clone.environmentId))
          const probed = Boolean(clone && events.some(event => isConfirmedUndo(event) && event.environmentId === clone.environmentId && events.some(action => action.kind === 'action' && action.environmentId === clone.environmentId && action.action?.id === event.undoId && action.tool === 'levers.apply')))
          const confirmed = workspace.confirmed && workspace.diagnosis === hypothesis.id
          return <article className="hypothesis-card" key={hypothesis.id} style={{ '--hypothesis-color': hypothesis.color } as React.CSSProperties}>
            <div className="hypothesis-top"><span className="hypothesis-letter">{hypothesis.id}</span><span className="overline">{confirmed ? (scenario.live ? 'CONFIRMED IN PRODUCTION' : 'CONFIRMED IN EXAMPLE') : observed ? 'REPRODUCED IN CLONE' : 'PROPOSED'}</span>{(confirmed || observed) && <Check size={13} />}</div>
            <h3>{hypothesis.title}</h3><p>{hypothesis.description}</p>
            <div className="prediction-line"><ArrowDownRight size={13} /><span>{hypothesis.prediction}</span></div>
            <div className="evidence-checks"><span className={cloned ? 'done' : ''}><i />Clone</span><span className={observed ? 'done' : ''}><i />Reproduce</span><span className={probed ? 'done' : ''}><i />Probe</span></div>
          </article>
        })}
        <div className="next-question"><span className="small-icon"><FlaskConical size={15} /></span><div><b>{workspace.verdict ? diagnosisSummary(scenario, workspace) : 'What response would distinguish these causes?'}</b><p>{workspace.verdict ?? 'Compare the predicted response with measurements during and after the intervention.'}</p></div></div>
        <div className="evidence-boundary"><span className="overline">HOW WE CHECK THE CAUSE</span><p>A clone can reproduce the symptoms without proving the cause. We still need to test the prediction in production.</p><button className="text-button" onClick={() => set({ traceTab: 'trace' })}>See the test history <ArrowRight size={13} /></button></div>
      </> : <>
        <div className="trace-filter">{selectedNode ? <span>Activity in {environment.label}</span> : <><label htmlFor="trace-filter">Environment</label><select id="trace-filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All environments</option><option value="production">Production</option>{createdClones.map(event => <option key={event.environmentId} value={event.environmentId}>{event.environment?.label}</option>)}</select></>}</div>
        <p className="trace-explanation">Follow what the agent tried and what it found. {scenario.live ? 'Every step is a recorded audit event.' : 'This trace is simulated.'}</p>
        {filtered.length === 0 && <p className="empty-copy">No activity here yet. Advance the replay or select another system.</p>}
        {filtered.map(event => <TraceStep key={event.id} live={scenario.live} event={event} selected={event.id === (filtered.some(item => item.id === selectedEvent) ? selectedEvent : latest?.id)} onSelect={() => { if (event.targetId && workspace.environments.some(env => env.id === event.environmentId)) inspect(event.targetId, event.environmentId); set({ selectedEvent: event.id }) }} />)}
      </>}
    </div>
    <div className="inspector-footer"><Clock3 size={12} /><span>Viewing evidence through {timeLabel(cursor)}</span><span>{scenario.live ? 'RECORDED' : 'SIMULATED'}</span></div>
  </aside>
}
