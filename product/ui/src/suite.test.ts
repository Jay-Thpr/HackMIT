import { describe, expect, it } from 'vitest'
import { uniformTimelineScenarios } from './scenarios'
import { suiteChecks } from './suite'
describe('recorded clone checks', () => {
  for (const scenario of uniformTimelineScenarios) it(`${scenario.id}: progresses only after results and rewinds`, () => {
    const states = (time: number) => suiteChecks(scenario, 'clone-a', time).map(check => check.state)
    expect(states(24)).toEqual(['running', 'queued', 'queued', 'queued'])
    expect(states(35)).toEqual(['passed', 'running', 'queued', 'queued'])
    expect(states(58)).toEqual(['passed', 'passed', 'running', 'queued'])
    expect(states(66)).toEqual(['passed', 'passed', 'passed', 'running'])
    expect(states(69)).toEqual(['passed', 'passed', 'passed', 'passed'])
    expect(suiteChecks(scenario, 'clone-a', 24).every(check => !check.event)).toBe(true)
    expect(suiteChecks(scenario, 'clone-b', 70)[3].event?.testResult?.observed).toContain('does not mean the system is healthy')
  })
})

it('keeps 1000 cases in four groups and never marks a partial group passed', () => {
  const scenario = structuredClone(uniformTimelineScenarios[0])
  scenario.testCases = Array.from({ length: 1000 }, (_, i) => ({ id: `test-${i}`, groupId: (['baseline','reproduction','probe','release'] as const)[i % 4], name: `Test ${i}`, description: 'Synthetic scale check' }))
  scenario.events.push(...scenario.testCases.slice(0,500).map((test,i) => ({ id: `result-${i}`, sequence: i, at: 50, kind: 'observe' as const, actor: 'math' as const, environmentId: 'clone-a', title: 'Result', detail: '', testResult: { checkId: test.groupId, caseId: test.id, passed: true, expected: 'Match', observed: 'Matched' } })))
  const groups = suiteChecks(scenario,'clone-a',50)
  expect(groups).toHaveLength(4)
  expect(groups.reduce((sum,group) => sum + group.total,0)).toBe(1000)
  expect(groups.reduce((sum,group) => sum + group.passed,0)).toBe(500)
  expect(groups.every(group => group.state !== 'passed')).toBe(true)
  expect(suiteChecks(scenario,'clone-a',49).every(group => group.passed === 0)).toBe(true)
})
