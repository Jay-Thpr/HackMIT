import { ArrowRight } from 'lucide-react'
import { useEffect, useState } from 'react'
import { diagnosisSummary, metricLabel, timeLabel, visibleEvents, type Scenario, type WorkspaceState, environmentOutcomeLabel } from '../model'
import { useWorkspace } from '../store'
import { suiteChecks } from '../suite'
import { MetricChart } from './MetricChart'

type SupportingTelemetry = {
  state: 'available' | 'not-configured' | 'unavailable'
  detail: string
  spans?: number
  traces?: number
  logs?: number
  services?: { name: string; spans: number }[]
}

function SupportingTelemetryPanel({ scenario }: { scenario: Scenario }) {
  const [evidence, setEvidence] = useState<SupportingTelemetry | null>(null)
  useEffect(() => {
    if (!scenario.live) { setEvidence(null); return }
    let active = true
    fetch(`/api/incidents/${encodeURIComponent(scenario.id)}/supporting-telemetry`)
      .then(response => response.ok ? response.json() as Promise<SupportingTelemetry> : null)
      .then(result => { if (active && result) setEvidence(result) })
      .catch(() => { if (active) setEvidence({ state: 'unavailable', detail: 'Observability evidence could not be read.' }) })
    return () => { active = false }
  }, [scenario.id, scenario.live])
  if (!scenario.live) return <section className="supporting-evidence"><span className="overline">SUPPORTING TELEMETRY</span><p>Live trace and log summaries appear here for recorded incidents. They are context for a person, not an input to the verdict.</p></section>
  if (!evidence) return <section className="supporting-evidence"><span className="overline">SUPPORTING TELEMETRY</span><p>Loading the bounded trace and log summary…</p></section>
  if (evidence.state !== 'available') return <section className="supporting-evidence"><span className="overline">SUPPORTING TELEMETRY</span><p>{evidence.detail}</p></section>
  return <section className="supporting-evidence" aria-label="Supporting telemetry"><div><span className="overline">SUPPORTING TELEMETRY</span><p>{evidence.detail}</p></div><dl><div><dt>Spans</dt><dd>{evidence.spans}</dd></div><div><dt>Traces</dt><dd>{evidence.traces}</dd></div><div><dt>Logs</dt><dd>{evidence.logs}</dd></div></dl>{evidence.services?.length ? <p className="telemetry-services">Observed services: {evidence.services.map(service => `${service.name} (${service.spans})`).join(' · ')}</p> : <p className="telemetry-services">No production spans were returned for this recorded window.</p>}<small>Trace and log summaries never change Faultline’s diagnosis.</small></section>
}

function IncidentMemory({ scenario, cursor }: { scenario: Scenario; cursor: number }) {
  const records = (scenario.memory ?? []).filter(item => item.recordedAt <= cursor)
  if (!records.length) return <section className="incident-memory"><span className="overline">INCIDENT MEMORY</span><p>{scenario.live ? 'No similar incidents were returned at triage.' : 'Similar recorded incidents appear here during a live investigation.'}</p></section>
  return <section className="incident-memory" aria-label="Incident memory"><div><span className="overline">INCIDENT MEMORY</span><p>Similar incidents are retrieval context only. They do not choose the diagnosis.</p></div><div className="memory-results">{records.map(item => <article key={item.incidentId}><strong>{item.incidentId}</strong><span>{Math.round(item.score * 100)}% similar</span><p>{item.confirmed && item.diagnosis ? `Recorded outcome: ${item.diagnosis}` : 'No confirmed outcome recorded.'}</p></article>)}</div></section>
}

export function Explanation({ scenario, workspace }: { scenario: Scenario; workspace: WorkspaceState }) {
  const { cursor, set, focus } = useWorkspace()
  const events = visibleEvents(scenario, cursor)
  const incident = events.some(event => event.kind === 'detect')
  const proposed = events.some(event => event.kind === 'reason')
  const production = workspace.environments[0]
  const reading = production.nodes[scenario.targetId]
  const conclusion = diagnosisSummary(scenario, workspace)
  const finalVerdict = scenario.events.filter(event => event.kind === 'verdict').at(-1)
  const actions = scenario.events.filter(event => event.kind === 'action' && event.environmentId === 'production')
  const undone = new Set(scenario.events.filter(event => event.kind === 'undo' && event.environmentId === 'production').map(event => event.undoId))
  const go = (environmentId: string) => { focus(environmentId); set({ view: 'investigation' }) }
  return <div className="explanation-page">
    <section className="why-intro"><div><span className="overline">WHAT WE KNOW · {timeLabel(cursor)}</span><h2>{workspace.verdict ? conclusion : incident ? 'We see the failure. We are still testing the cause.' : 'Start with a healthy reference.'}</h2><p>{workspace.verdict ?? (incident ? `${scenario.targetId} is showing high latency and errors. Those symptoms alone cannot tell us whether retries are sustaining the overload or the dependency has lost capacity.` : 'There is no incident recorded yet. These measurements show how the system normally behaves.')}</p></div><button className="secondary-button" onClick={() => go('production')}>Watch the agents in 3D <ArrowRight size={14} /></button></section>
    <section className="why-signals" aria-label="Production symptoms">{[['Dependency latency', metricLabel(reading?.latency, 'ms'), `Healthy reference: ${metricLabel(scenario.baseline[scenario.targetId]?.latency, 'ms')}`], ['Error rate', metricLabel(reading?.errorRate, '%'), 'Requests reporting an error'], ['Issued load', metricLabel(reading?.qps, 'qps'), 'Includes repeated requests']].map(([label,value,note]) => <div key={label}><span>{label}</span><strong>{value}</strong><small>{note}</small></div>)}</section>
    <section className="why-chart panel"><div className="panel-title"><div><span className="overline">WHAT CHANGED IN PRODUCTION</span><h3>{scenario.targetId}</h3></div><span className="chart-legend"><i className="latency-line" />Latency · ms<i className="load-line" />Load · qps</span></div><MetricChart scenario={scenario} cursor={cursor} environmentId="production" nodeId={scenario.targetId} /><p className="chart-footnote">Shaded spans show interventions. Only evidence available at this replay time is shown. {scenario.live ? 'Readings come from recorded C1 windows and C4 audit events.' : 'All values are simulated.'}</p></section>
    <div className="evidence-context"><IncidentMemory scenario={scenario} cursor={cursor} /><SupportingTelemetryPanel scenario={scenario} /></div>
    <section className="why-method"><span className="overline">WHY INTRODUCE A FAILURE?</span><h2>To find out which explanation survives a test.</h2><p>A chart can show that the system is overloaded without explaining why. We introduce one controlled change in a clean clone, then check whether it produces the same symptoms. Next, we cap retries and remove the cap. The two possible causes predict different responses.</p><ol className="why-flow"><li><b>Reproduce</b><span>Can the suspected cause create these symptoms?</span></li><li><b>Probe</b><span>What changes when we limit retries?</span></li><li><b>Release</b><span>Does recovery last when retries return?</span></li><li><b>Confirm</b><span>Does production respond as predicted?</span></li></ol><p className="why-boundary">Clone experiments use isolated state and reversible changes. A matching clone result keeps a theory in play; it does not prove what caused the production incident.</p></section>
    <section className="why-hypothesis panel"><span className="overline">MEASURED VERDICT & SAFETY LEDGER</span><h2>Why the alternatives did not pass</h2>{scenario.hypotheses.filter(item => item.id !== workspace.diagnosis).map(item => <p key={item.id}><b>{item.id} · {item.title}:</b> it was not confirmed because its own positive prediction was not satisfied by the measured probe. Historical matches and model reasoning cannot override that requirement.</p>)}{finalVerdict?.result && <p className="chart-footnote">Measured observations: {finalVerdict.result}</p>}<dl>{actions.map(action => <div key={action.action?.id}><dt>{action.action?.label}</dt><dd>Production action · TTL {action.action?.ttl}s · {undone.has(action.action?.id) ? 'undo recorded' : 'undo not yet recorded'} · blast radius is evaluated by the planner before apply.</dd></div>)}</dl><p className="chart-footnote">All actions are auditable and bounded; clones do not affect production requests.</p></section>
    <div className="why-comparisons">{proposed ? scenario.hypotheses.map(hypothesis => {
      const envId = events.find(event => event.kind === 'clone' && event.environment?.hypothesisId === hypothesis.id)?.environmentId
      const environment = workspace.environments.find(env => env.id === envId)
      const perturbation = events.find(event => event.environmentId === envId && event.tool === 'lab.apply')
      const checks = envId ? suiteChecks(scenario, envId, cursor) : []
      return <section className="why-hypothesis panel" key={hypothesis.id}><span className="overline">POSSIBLE CAUSE {hypothesis.id}</span><h2>{hypothesis.title}</h2><p>{hypothesis.description}</p><dl><div><dt>Chaos test</dt><dd>{perturbation?.title ?? 'No clone action recorded yet.'}</dd></div><div><dt>Why this test?</dt><dd>{perturbation?.prediction ?? hypothesis.description}</dd></div><div><dt>What would we expect?</dt><dd>{hypothesis.prediction}</dd></div><div><dt>At this point</dt><dd>{perturbation ? `The change was applied at ${timeLabel(perturbation.at)} with a ${perturbation.action?.ttl}s time limit.` : 'The change has not been applied yet.'}</dd></div></dl>
        <div className="why-checks">{checks.map(check => <details key={check.id}><summary><i className={check.state === 'passed' ? 'passed' : ''} /><span>{check.label}</span><small>{check.state === 'passed' ? 'Passed' : check.state === 'running' ? 'Running' : check.state === 'failed' ? 'Failed' : 'Not run'}</small></summary><p>{check.description}</p>{check.event?.testResult && <p><b>Result:</b> {check.event.testResult.observed}</p>}</details>)}</div>
        {environment && envId ? <><h3 className="why-chart-label">How {environment.label} responded{environment.outcome ? ` · ${environmentOutcomeLabel[environment.outcome]}` : ''}</h3><MetricChart scenario={scenario} cursor={cursor} environmentId={envId} nodeId={scenario.targetId} /><button className="text-button" onClick={() => go(envId)}>{environment.lifecycle === 'archived' ? 'Review this clone' : 'Watch this agent'} <ArrowRight size={14} /></button>{environment.lifecycle === 'archived' && <p className="chart-footnote">Clone archived. Its test results are retained above.</p>}</> : <p className="chart-footnote">The clone has not been created yet.</p>}
      </section>
    }) : <p className="empty-copy">Possible causes will appear when the agent has reviewed the incident.</p>}</div>
    <section className="why-conclusion"><span className="overline">WHAT CAN WE CONCLUDE?</span><h2>{conclusion}</h2><p>{workspace.verdict ?? 'Passing clone tests tells us whether our predictions match those experiments. We still need a measured production response before choosing a cause.'}</p>{workspace.confirmed && <p>A confirmed diagnosis does not by itself establish that mitigation or repair is complete.</p>}</section>
  </div>
}
