import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, GitBranch, LockKeyhole, ShieldCheck, X } from 'lucide-react'
import { visibleEvents, type Scenario, type WorkspaceState } from '../model'
import { useWorkspace } from '../store'

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
  return <dialog ref={element} className="workspace-dialog" onCancel={close} onClose={close} aria-labelledby="dialog-title" onClick={event => { if (event.target === element.current) { const bounds = element.current!.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close() } }}>
    <div className="dialog-header"><span className="overline">{dialog === 'experiment' ? 'LOCAL DRAFT' : 'SAFETY & APPROVALS'}</span><button className="icon-button" aria-label="Close dialog" onClick={close}><X size={18} /></button></div>
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
    </form> : <>
      <h2 id="dialog-title">Autonomy with boundaries.</h2><p className="dialog-subtitle">A design preview of execution boundaries. No live permissions or approvals are connected.</p>
      <div className="safety-tier"><ShieldCheck size={17} /><div><strong>Proposed automatic tier</strong><p>Telemetry reads and bounded, reversible interventions.</p></div></div>
      <div className="safety-tier"><GitBranch size={17} /><div><strong>Canary-gated</strong><p>Code changes require replay verification and a measured rollout.</p></div></div>
      <div className="safety-tier human"><LockKeyhole size={17} /><div><strong>Human-gated</strong><p>Shard splits, capacity changes, and irreversible operations.</p></div></div>
      <section className="proposal-preview"><div className="proposal-heading"><span className="overline">EXAMPLE PROPOSAL</span><span className="quiet-badge">Not applied</span></div><h3>Split a hot shard</h3><div className="ghost-shards"><span>Current shard</span><ArrowRight size={18} /><div><span>Proposed A</span><span>Proposed B</span></div></div><p>A real case must include target identity, measured evidence, a migration runbook, recovery constraints, and an approver. This example is not a recommendation for the current incident.</p><button className="secondary-button full-width" disabled>Approval unavailable · read-only workspace</button></section>
      <div className="dialog-notice"><LockKeyhole size={16} /><span>“Pause simulation” stops only this UI’s playback. It is not a production kill switch and does not undo infrastructure actions.</span></div>
    </>}
  </dialog>
}
