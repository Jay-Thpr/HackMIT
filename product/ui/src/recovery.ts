import { isConfirmedVerdict, visibleEvents, type Environment, type Scenario } from './model'

export type NodeRecovery = 'healthy' | 'degraded' | 'recovering' | 'unknown'

/** Healthy readings after an incident remain provisional until this environment has a measured confirmation. */
export function deriveNodeRecovery(scenario: Scenario, environment: Environment, nodeId: string, time: number): NodeRecovery {
  const health = environment.nodes[nodeId]?.health
  if (health === 'degraded') return 'degraded'
  if (health !== 'healthy') return 'unknown'
  const events = visibleEvents(scenario, time).filter(event => event.environmentId === environment.id)
  let lastDegradation = -1
  events.forEach((event, index) => { if (event.readings?.[nodeId]?.health === 'degraded') lastDegradation = index })
  if (lastDegradation < 0) return 'healthy'
  const verdict = events.slice(lastDegradation + 1).filter(event => event.kind === 'verdict' && event.tool === 'judge.confirm').at(-1)
  return verdict && isConfirmedVerdict(verdict) ? 'healthy' : 'recovering'
}
