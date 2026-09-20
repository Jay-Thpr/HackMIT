import { visibleEvents, type Scenario } from './model'

export function agentActivity(scenario: Scenario, environmentId: string, cursor: number) {
  const events = visibleEvents(scenario, cursor).filter(event => event.environmentId === environmentId)
  // A read-only responder never applies or undoes anything, so its activity is described by
  // what it read and what it concluded, not by a test it is running.
  if (events.some(event => event.actor === 'elastic')) {
    const conclusion = events.filter(event => event.actor === 'elastic' && (event.kind === 'reason' || event.kind === 'verdict')).at(-1)
    const read = events.filter(event => event.actor === 'elastic' && event.kind === 'observe').at(-1)
    const abstained = conclusion?.abstained === true
    return {
      name: 'Read-only responder',
      event: conclusion ?? read,
      targetId: conclusion?.targetId ?? read?.targetId ?? scenario.targetId,
      phase: conclusion ? abstained ? 'Declined to name a cause' : 'Concluded' : 'Reading telemetry',
      why: conclusion?.detail ?? read?.detail ?? 'Compare the recent windows with the healthy history.',
      waiting: !conclusion ? 'Reading the recorded windows; no action is taken.'
        : abstained ? 'The recorded evidence does not separate the candidates. Nothing further can be read.'
        : 'This conclusion was read from telemetry. It was not tested against the system.',
    }
  }
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
