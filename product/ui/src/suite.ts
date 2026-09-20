import { visibleEvents, type Scenario } from './model'

const definitions = [
  { id: 'baseline', label: 'Clean baseline', tool: 'lab.create' },
  { id: 'reproduction', label: 'Incident reproduced', tool: 'lab.apply' },
  { id: 'probe', label: 'Probe prediction', tool: 'levers.apply' },
  { id: 'release', label: 'Release prediction', tool: 'levers.undo' },
]
export function suiteChecks(scenario: Scenario, environmentId: string, cursor: number) {
  const events = visibleEvents(scenario, cursor).filter(event => event.environmentId === environmentId)
  return definitions.map(check => {
    const event = events.filter(event => event.testResult?.checkId === check.id).at(-1)
    const state = event?.testResult ? event.testResult.passed ? 'passed' : 'failed' : events.some(event => event.tool === check.tool) ? 'running' : 'queued'
    const descriptions: Record<string, string> = {
      baseline: 'Check that the new clone starts with normal latency, load, and error rates.',
      reproduction: 'Apply the suspected cause in the clone and check whether it produces the same symptoms as production.',
      probe: environmentId === 'clone-a' ? 'Cap retries and check whether latency returns to normal.' : 'Cap retries and check whether latency stays high even as load falls.',
      release: environmentId === 'clone-a' ? 'Restore retries and check whether the system stays healthy.' : 'Restore retries and check whether overload returns, as this hypothesis predicts.',
    }
    const manifest = scenario.testCases?.filter(test => test.groupId === check.id) ?? [{ id: check.id, groupId: check.id, name: check.label, description: descriptions[check.id] }]
    const cases = manifest.map(test => {
      const result = events.filter(item => item.testResult?.checkId === check.id && (item.testResult.caseId ?? item.testResult.checkId) === test.id).at(-1)
      return { ...test, event: result, state: result?.testResult ? result.testResult.passed ? 'passed' : 'failed' : state === 'queued' ? 'queued' : 'running' }
    })
    const passed = cases.filter(test => test.state === 'passed').length
    const failed = cases.filter(test => test.state === 'failed').length
    const groupState = cases.length > 0 && passed === cases.length ? 'passed' : failed > 0 ? 'failed' : cases.some(test => test.state === 'running') ? 'running' : 'queued'
    return { ...check, description: descriptions[check.id], state: groupState, event: cases.length === 1 ? cases[0].event : undefined, cases, passed, failed, total: cases.length }
  })
}
