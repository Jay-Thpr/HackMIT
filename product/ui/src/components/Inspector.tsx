import { ArrowDownRight, ArrowRight, Check, ChevronDown, ChevronRight, Clock3, FlaskConical, ScanLine, Terminal, X } from 'lucide-react'
import { useState } from 'react'
import { metricLabel, timeLabel, visibleEvents, type Environment, type Scenario, type WorkspaceEvent, type WorkspaceState } from '../model'
import { useWorkspace } from '../store'

const actorLabel: Record<WorkspaceEvent['actor'], string> = { model: 'Model proposal', math: 'Measured evaluation', adapter: 'Tool execution', 'investigator-a': 'Investigator A', 'investigator-b': 'Investigator B', orchestrator: 'Orchestrator' }

function TraceStep({ event, selected, onSelect }: { event: WorkspaceEvent; selected: boolean; onSelect: () => void }) {
  return <div className={`trace-step ${selected ? 'expanded' : ''}`}>
    <button className="trace-step-heading" onClick={onSelect} aria-expanded={selected}>
      <span className={`trace-glyph ${event.actor === 'math' ? 'math' : ''}`}>{event.kind === 'observe' ? <ScanLine size={13} /> : event.kind === 'action' ? <Terminal size={13} /> : event.kind === 'verdict' ? <Check size={13} /> : <FlaskConical size={13} />}</span>
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
      <span className="data-caption">Scripted example · not a live model call</span>
    </div>}
  </div>
}

export function Inspector({ scenario, workspace, environment }: { scenario: Scenario; workspace: WorkspaceState; environment: Environment }) {
  const { cursor, selectedNode, selectedEvent, traceTab, inspect, set } = useWorkspace()
  const events = visibleEvents(scenario, cursor)
  const [filter, setFilter] = useState('all')
  const node = scenario.topology.nodes.find(item => item.id === selectedNode)
  const reading = node && environment.nodes[node.id]
  const filtered = events.filter(event => (filter === 'all' || event.environmentId === filter) && (!selectedNode || event.targetId === selectedNode || event.kind === 'clone'))
  const createdClones = events.filter(event => event.kind === 'clone')
  const latest = filtered.at(-1)
  return <aside className="inspector panel">
    <div className="panel-title inspector-heading"><span className="overline">{node ? 'ENTITY INSPECTOR' : 'INVESTIGATION'}</span><span className="quiet-badge">{scenario.incident}</span></div>
    {node && <div className="selected-entity">
      <div><strong>{node.label}</strong><button className="icon-button" aria-label="Close entity inspector" onClick={() => set({ selectedNode: undefined })}><X size={15} /></button></div>
      <p>{environment.label} · {node.kind} · {node.instrumented ? 'Instrumented' : 'Observed dependency'}</p>
      <div className="entity-metrics"><span>p99 latency<b>{metricLabel(reading?.latency, 'ms')}</b></span><span>Issued load<b>{metricLabel(reading?.qps, 'qps')}</b></span></div>
      <div className="entity-health"><i className={`health-dot ${reading?.health ?? 'unknown'}`} />{reading?.health ?? 'unknown'}<span>Instances: {node.instances ?? 'not collected'}</span></div>
    </div>}
    <div className="tab-bar" role="tablist" aria-label="Investigation details">
      <button role="tab" aria-selected={traceTab === 'evidence'} onClick={() => set({ traceTab: 'evidence' })}>Evidence</button>
      <button role="tab" aria-selected={traceTab === 'trace'} onClick={() => set({ traceTab: 'trace' })}>Decision trace <span>{events.length}</span></button>
    </div>
    <div className="inspector-body">
      {traceTab === 'evidence' ? <>
        <div className="evidence-intro"><span className="overline">{cursor < 18 ? 'OBSERVATION' : 'WORKING HYPOTHESES'}</span><p>{cursor < 12 ? 'Establish a healthy reference before interpreting a change.' : workspace.verdict ? 'One hypothesis passed its production confirmation test in this example.' : 'The symptoms agree. The explanations don’t.'}</p></div>
        {cursor >= 18 && scenario.hypotheses.map((hypothesis, index) => {
          const cloned = createdClones.some(event => event.environment?.hypothesisId === hypothesis.id)
          const observed = events.some(event => event.kind === 'observe' && event.environmentId === `clone-${index === 0 ? 'a' : 'b'}`)
          const probed = events.some(event => event.tool === 'levers.undo' && event.environmentId === `clone-${index === 0 ? 'a' : 'b'}`)
          return <article className="hypothesis-card" key={hypothesis.id} style={{ '--hypothesis-color': hypothesis.color } as React.CSSProperties}>
            <div className="hypothesis-top"><span className="hypothesis-letter">{hypothesis.id}</span><span className="overline">{workspace.verdict && index === 0 ? 'CONFIRMED IN EXAMPLE' : observed ? 'REPRODUCED IN CLONE' : 'PROPOSED'}</span>{observed && <Check size={13} />}</div>
            <h3>{hypothesis.title}</h3><p>{hypothesis.description}</p>
            <div className="prediction-line"><ArrowDownRight size={13} /><span>{hypothesis.prediction}</span></div>
            <div className="evidence-checks"><span className={cloned ? 'done' : ''}><i />Clone</span><span className={observed ? 'done' : ''}><i />Reproduce</span><span className={probed ? 'done' : ''}><i />Probe</span></div>
          </article>
        })}
        <div className="next-question"><span className="small-icon"><FlaskConical size={15} /></span><div><b>{workspace.verdict ? 'The confirmation is in the recovery.' : 'The question that separates them'}</b><p>Does the system stay healthy after the intervention is released?</p></div></div>
        <div className="evidence-boundary"><span className="overline">MEASUREMENT, NOT CONFIDENCE</span><p>Reproducing a symptom keeps a hypothesis in contention. Only a positive production test can confirm it.</p><button className="text-button" onClick={() => set({ traceTab: 'trace' })}>Inspect the evidence chain <ArrowRight size={13} /></button></div>
      </> : <>
        <div className="trace-filter"><label htmlFor="trace-filter">Environment</label><select id="trace-filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All environments</option><option value="production">Production</option>{createdClones.map(event => <option key={event.environmentId} value={event.environmentId}>{event.environment?.label}</option>)}</select></div>
        <p className="trace-explanation">Proposals, tool calls, and returned evidence — in execution order. All entries are scripted examples.</p>
        {filtered.length === 0 && <p className="empty-copy">No events for this entity at the selected time.</p>}
        {filtered.map(event => <TraceStep key={event.id} event={event} selected={event.id === (selectedEvent ?? latest?.id)} onSelect={() => { if (event.targetId && workspace.environments.some(env => env.id === event.environmentId)) inspect(event.targetId, event.environmentId); set({ selectedEvent: event.id }) }} />)}
      </>}
    </div>
    <div className="inspector-footer"><Clock3 size={12} /><span>Viewing evidence through {timeLabel(cursor)}</span><span>SIMULATED</span></div>
  </aside>
}
