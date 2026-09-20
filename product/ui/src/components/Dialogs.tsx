import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, FileText, GitBranch, Layers3, LayoutDashboard, LockKeyhole, Network, ShieldCheck, X } from 'lucide-react'
import { visibleEvents, type Scenario, type WorkspaceState } from '../model'
import { useWorkspace, type View } from '../store'

const guideTabs: { view: View; title: string; description: string; icon: typeof LayoutDashboard }[] = [
  { view: 'investigation', title: 'Agent workspace', description: 'Play the incident timeline, inspect the 3D system, and watch agents test causes in isolated clones.', icon: Network },
  { view: 'observability', title: 'Observability', description: 'Check service health, latency, errors, and dependency context before entering an investigation.', icon: LayoutDashboard },
  { view: 'replay', title: 'Incident replay', description: 'Choose a recorded investigation, replay it from the beginning, and review its final report.', icon: Layers3 },
  { view: 'elastic', title: 'Evidence lineage', description: 'See how OpenTelemetry signals, Elastic evidence, bounded retrieval, and the decision record connect.', icon: GitBranch },
]

export function Dialogs({ scenario, workspace }: { scenario: Scenario; workspace: WorkspaceState }) {
  const { dialog, cursor, set } = useWorkspace()
  const element = useRef<HTMLDialogElement>(null)
  const hypothesesAvailable = scenario.hypotheses.length > 0 && visibleEvents(scenario, cursor).some(event => event.kind === 'reason')
  const [saved, setSaved] = useState(false)
  const [hypothesisId, setHypothesisId] = useState('')
  const [prediction, setPrediction] = useState('')
  useEffect(() => {
    if (dialog) { setSaved(false); setHypothesisId(hypothesesAvailable ? scenario.hypotheses[0].id : ''); setPrediction(hypothesesAvailable ? scenario.hypotheses[0].prediction : ''); element.current?.showModal() }
    else element.current?.close()
  }, [dialog, scenario.id, hypothesesAvailable])
  const close = () => set({ dialog: null })
  const dialogLabel = dialog === 'experiment' ? 'LOCAL DRAFT' : dialog === 'report' ? 'INCIDENT REPORT' : dialog === 'guide' ? 'JUDGE GUIDE' : 'SAFETY & APPROVALS'
  return <dialog ref={element} className={`workspace-dialog ${dialog === 'report' ? 'report-sheet' : ''} ${dialog === 'guide' ? 'judge-guide' : ''}`} onCancel={close} onClose={close} aria-labelledby="dialog-title" onClick={event => { if (event.target === element.current) { const bounds = element.current!.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close() } }}>
    <div className="dialog-header"><span className="overline">{dialogLabel}</span><button className="icon-button" aria-label="Close dialog" onClick={close}><X size={18} /></button></div>
    {dialog === 'experiment' ? <form key={scenario.id} onChange={() => setSaved(false)} onSubmit={event => { event.preventDefault(); setSaved(true) }}>
      <h2 id="dialog-title">Draft an experiment</h2><p className="dialog-subtitle">Describe a possible cause and the response that would test it. Validation checks required fields and the time limit, not whether the experiment is safe or correct.</p>
      <label>Hypothesis to test{hypothesesAvailable ? <select required value={hypothesisId} onChange={event => { setHypothesisId(event.target.value); setPrediction(scenario.hypotheses.find(item => item.id === event.target.value)!.prediction) }}>{scenario.hypotheses.map(hypothesis => <option key={hypothesis.id} value={hypothesis.id}>{hypothesis.title}</option>)}</select> : <input required value={hypothesisId} onChange={event => setHypothesisId(event.target.value)} placeholder="Describe a possible cause" aria-describedby="draft-hypothesis-help" />}</label>
      {!hypothesesAvailable && <p className="form-help" id="draft-hypothesis-help">No causes have been proposed at this point. Write your own draft, or review the incident first.</p>}
      <div className="form-row"><label>Planned environment<select defaultValue="new"><option value="new">New isolated clone (planned)</option>{workspace.environments.filter(env => env.id !== 'production' && env.lifecycle !== 'destroying').map(env => <option key={env.id} value={env.id}>{env.label}</option>)}</select></label><label>Target entity<select defaultValue={scenario.targetId}>{scenario.topology.nodes.map(node => <option key={node.id} value={node.id}>{node.label}</option>)}</select></label></div>
      <label>Predicted response<textarea required placeholder="If this cause is correct, changing… should result in…" value={prediction} onChange={event => setPrediction(event.target.value)} rows={3} /></label>
      <div className="form-row"><label>Time limit (seconds)<input type="number" min={1} max={120} defaultValue={20} required aria-describedby="draft-time-help" /></label><label>Stop condition<input defaultValue="Any unexpected regression" required /></label></div>
      <p className="form-help" id="draft-time-help">Choose 1–120 seconds for the proposed intervention.</p>
      <div className="dialog-notice"><LockKeyhole size={16} /><span>Draft only. Validation does not save the plan, create a clone, or run a test.</span></div>
      <button className="primary-button full-width" type="submit">{saved ? <><Check size={15} />Draft validated locally</> : <>Validate draft <ArrowRight size={15} /></>}</button>
      {saved && <p className="form-success" role="status">Required fields and time limit are valid. Nothing was saved or executed. Editing a field clears this validation.</p>}
    </form> : dialog === 'report' ? <IncidentReport scenario={scenario} workspace={workspace} close={close} /> : dialog === 'guide' ? <JudgeGuide close={close} /> : <>
      <h2 id="dialog-title">Autonomy with boundaries.</h2><p className="dialog-subtitle">Execution boundaries for the selected incident. Production permissions remain read-only.</p>
      <div className="safety-tier"><ShieldCheck size={17} /><div><strong>Proposed automatic tier</strong><p>Telemetry reads and bounded, reversible interventions.</p></div></div>
      <div className="safety-tier"><GitBranch size={17} /><div><strong>Canary-gated</strong><p>Code changes require replay verification and a measured rollout.</p></div></div>
      <div className="safety-tier human"><LockKeyhole size={17} /><div><strong>Human-gated</strong><p>Shard splits, capacity changes, and irreversible operations.</p></div></div>
      <section className="proposal-preview"><div className="proposal-heading"><span className="overline">READ-ONLY PROPOSAL</span><span className="quiet-badge">Not applied</span></div><h3>Split a hot shard</h3><div className="ghost-shards"><span>Current shard</span><ArrowRight size={18} /><div><span>Proposed A</span><span>Proposed B</span></div></div><p>A production proposal must include target identity, measured evidence, a migration runbook, recovery constraints, and an approver. This proposal is not a recommendation for the current incident.</p><button className="secondary-button full-width" disabled>Approval unavailable · read-only workspace</button></section>
      <div className="dialog-notice"><LockKeyhole size={16} /><span>“Pause replay” stops only this UI’s playback. It is not a production kill switch and does not undo infrastructure actions.</span></div>
    </>}
  </dialog>
}

function JudgeGuide({ close }: { close: () => void }) {
  const { set } = useWorkspace()
  const open = (view: View) => { close(); set({ view, explanationOpen: false }) }
  return <div className="judge-guide-content">
    <h2 id="dialog-title">What each workspace tab does</h2>
    <p className="dialog-subtitle">Faultline turns incident telemetry into an inspectable investigation. Use these four views to follow the complete story.</p>
    <ol className="judge-guide-tabs" aria-label="Workspace tab guide">
      {guideTabs.map(({ view, title, description, icon: Icon }, index) => <li key={view}>
        <span className="judge-guide-number">0{index + 1}</span>
        <Icon size={19} strokeWidth={1.6} />
        <div><h3>{title}</h3><p>{description}</p></div>
        <button className="text-button" aria-label={`Open ${title}`} onClick={() => open(view)}>Open <ArrowRight size={13} /></button>
      </li>)}
    </ol>
    <button className="primary-button full-width" onClick={() => open('investigation')}>Start in Agent workspace <ArrowRight size={15} /></button>
  </div>
}


function IncidentReport({ scenario, workspace, close }: { scenario: Scenario; workspace: WorkspaceState; close: () => void }) {
  const { cursor, set } = useWorkspace()
  const shown = visibleEvents(scenario, cursor)
  const verdict = shown.filter(event => event.kind === 'verdict' && event.environmentId === 'production').at(-1)
  const report = scenario.report
  const winner = workspace.environments.find(env => env.id === workspace.winner)
  const confirmedClone = workspace.environments.find(env => env.outcome === 'confirmed')
  const fixClone = workspace.environments.find(env => env.outcome === 'fix-verified' || env.outcome === 'fix-failed')
  const productionActions = shown.filter(event => event.kind === 'action' && event.environmentId === 'production').length
  const hypothesis = scenario.hypotheses.find(item => item.id === workspace.diagnosis)
  // Three outcomes, not two. An abstention is a run that did not resolve; a healthy control
  // resolved correctly by finding nothing. Both must be distinguishable from a confirmation.
  const outcome = workspace.confirmed ? 'confirmed'
    : scenario.report?.outcome === 'no_incident' || workspace.diagnosis === 'no_incident' ? 'no_incident'
    : 'unresolved'
  const headline = outcome === 'confirmed' ? (verdict?.title ?? workspace.verdict ?? 'Cause confirmed')
    : outcome === 'no_incident' ? 'No incident: nothing was wrong'
    : 'No cause confirmed'
  const because = outcome === 'confirmed' ? null
    : outcome === 'no_incident'
      ? 'The breach never met the sustained detection gate, so no hypothesis was raised and nothing was changed in production.'
      : (verdict?.detail ?? 'No hypothesis passed a test it could have failed, so measurement did not name a cause. A human was paged.')
  const facts: [string, string][] = [
    ['Diagnosis', workspace.diagnosis ? `${hypothesis?.title ?? workspace.diagnosis}${workspace.confirmed ? ' · confirmed' : ' · not confirmed'}` : 'No verdict yet'],
    ['Confirmed in', outcome === 'confirmed' ? (confirmedClone ? `${confirmedClone.label}, then production` : 'Production') : 'Not confirmed'],
    ['Production actions', `${productionActions}${report ? ` of ${report.productionActions}` : ''}, each with a TTL and a recorded undo`],
    ['Durable fix', report?.patch ? `${report.patchProvider ?? 'patch'} · ${report.patch}${report.patchRevision ? ` · revision ${report.patchRevision}` : ''}` : 'No patch recorded'],
    ['Fix verified', fixClone ? `${fixClone.label}: ${fixClone.outcome === 'fix-verified' ? 'survived the replayed incident' : 'did not survive the replay'}` : report?.verification ?? 'Not run'],
    ['Canary', report?.canary ?? 'Not run'],
  ]
  if (report?.mitigationHeld) facts.push(['Mitigation held', `${report.mitigationHeld} is holding production up; a human must fix the cause before its TTL ends`])
  return <div className="report-dialog">
    <div className="report-dialog-heading"><FileText size={18} /><div><h2 id="dialog-title">{headline}</h2><p className="dialog-subtitle">{report?.outcome ?? 'Recorded investigation outcome'} · {scenario.live ? `audit log · ${scenario.id}` : scenario.incident}</p></div></div>
    {winner && <p className="report-winner" data-environment={winner.id}><strong>{winner.label}</strong> is emphasised in the workspace: {winner.outcome === 'fix-verified' ? 'the patched clone survived the replayed incident.' : 'its reproduction matched the cause production confirmed.'}</p>}
    {because && <p className="report-outcome" data-outcome={outcome}><strong>{outcome === 'no_incident' ? 'Correctly found no incident.' : 'This investigation did not resolve.'}</strong> {because}</p>}
    <dl className="report-dialog-facts">{facts.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    {verdict?.result && <p className="report-dialog-evidence">{verdict.result}</p>}
    <div className="report-dialog-actions">
      <button className="primary-button" onClick={() => { close(); set({ view: 'replay' }) }}>Full report <ArrowRight size={15} /></button>
      <button className="secondary-button" onClick={() => { close(); set({ view: 'investigation', explanationOpen: true }) }}>Why this incident?</button>
      <button className="secondary-button" onClick={close}>Stay in the workspace</button>
    </div>
  </div>
}
