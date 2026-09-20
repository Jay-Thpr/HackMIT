import { visibleEvents, type Scenario } from './model'

export function agentActivity(scenario: Scenario, environmentId: string, cursor: number) {
  const events = visibleEvents(scenario, cursor).filter(event => event.environmentId === environmentId)
  const event = events.filter(event => event.targetId && ['reason','action','observe','undo','verdict'].includes(event.kind)).at(-1)
  const targetId = event?.targetId ?? scenario.targetId
  const creation = events.find(item => item.kind === 'clone')
  const hypothesis = scenario.hypotheses.find(item => item.id === creation?.environment?.hypothesisId)
  const name = environmentId === 'production' ? 'Production agent' : hypothesis ? `Investigator ${hypothesis.id}` : `Agent · ${creation?.environment?.label ?? environmentId}`
  const phase = event?.kind === 'action' ? 'Testing' : event?.kind === 'undo' ? 'Watching recovery' : event?.kind === 'verdict' ? 'Complete' : 'Observing'
  const why = event?.prediction ?? hypothesis?.description ?? 'Compare the recorded response with the investigation predictions.'
  const waiting = event?.kind === 'verdict' ? 'This investigation has a recorded conclusion.' : event?.kind === 'action' ? 'Collect measurements, then undo the change before judging the result.' : event?.kind === 'undo' ? 'Check whether recovery lasts after the change is removed.' : 'Compare the next measurement with the healthy reference.'
  return { name, event, targetId, phase, why, waiting }
}
