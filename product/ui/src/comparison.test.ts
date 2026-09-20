import { describe, expect, it } from 'vitest'
import { metricPoints, parseRecording, RecordingError, visibleRunEvents, type Recording, type Run } from './comparison'

function baseRecording(): Record<string, unknown> {
  return {
    schema_version: 'faultline-comparison/1',
    id: 'cmp-test',
    title: 'Test recording',
    created_at: '2026-01-01T00:00:00Z',
    protocol: { window_s: 5, horizon_s: 120 },
    cases: [
      { id: 'case-a', label: 'Retry storm', world: 'storm', expected: 'H_meta', workload_rps: 80, params: {} },
    ],
    runs: [
      {
        id: 'run-1', case_id: 'case-a', arm: 'probe', source: 'live', status: 'completed',
        started_at: null, duration_s: 120,
        samples: [
          { start_s: 0, end_s: 5, metrics: { 'svc.gateway.qps': 80, 'svc.gateway.error_rate': 0.1 }, slo_breached: false },
          { start_s: 10, end_s: 15, metrics: { 'svc.gateway.qps': 81 }, slo_breached: true },
        ],
        events: [
          { id: 'e1', at_s: 30, kind: 'detect', title: 'Detected', detail: '', environment: 'observer', evidence: 'observed', reference: null, diagnosis: null, action_id: null, ttl_s: null },
          { id: 'e2', at_s: 30, kind: 'hypothesis', title: 'Second at same time', detail: '', environment: 'observer', evidence: 'inferred', reference: null, diagnosis: null, action_id: null, ttl_s: null },
          { id: 'e3', at_s: 90, kind: 'conclusion', title: 'Late conclusion', detail: '', environment: 'observer', evidence: 'measured', reference: null, diagnosis: 'H_meta', action_id: null, ttl_s: null },
        ],
        metrics: { diagnosis: 'H_meta', correct: true, detection_s: 30, first_correct_s: 90, recovery_s: 60, recovery_status: 'recovered', failed_checkouts_estimate: 10, successful_checkouts_estimate: 90, sample_coverage_pct: 100, production_actions: 1, clone_actions: 0, rollback_failures: 0, tokens: 5000, cost_usd: null },
        provenance: { model: 'gpt-4.1' }, warnings: [],
      },
    ],
  }
}

function recording(): Recording {
  return parseRecording(baseRecording())
}

function run(): Run {
  return recording().runs[0]
}

describe('parseRecording', () => {
  it('accepts a minimal valid recording', () => {
    const parsed = recording()
    expect(parsed.id).toBe('cmp-test')
    expect(parsed.protocol.horizon_s).toBe(120)
    expect(parsed.runs[0].metrics?.diagnosis).toBe('H_meta')
    expect(parsed.protocol.baseline_s).toBe(130)
  })

  it('rejects a wrong schema_version', () => {
    expect(() => parseRecording({ ...baseRecording(), schema_version: 'other/2' })).toThrow(RecordingError)
  })

  it('rejects non-finite numbers', () => {
    const bad = baseRecording()
    ;(bad.runs as Record<string, unknown>[])[0].samples = [{ start_s: 0, end_s: Number.NaN, metrics: {}, slo_breached: null }]
    expect(() => parseRecording(bad)).toThrow(RecordingError)
    const badMetric = baseRecording()
    ;(badMetric.runs as Record<string, unknown>[])[0].samples = [{ start_s: 0, end_s: 5, metrics: { 'svc.gateway.qps': Number.POSITIVE_INFINITY }, slo_breached: null }]
    expect(() => parseRecording(badMetric)).toThrow(RecordingError)
  })

  it('rejects malformed nested samples and events', () => {
    const bad = baseRecording()
    ;(bad.runs as Record<string, unknown>[])[0].samples = [{ start_s: 'x', end_s: 5, metrics: {}, slo_breached: null }]
    expect(() => parseRecording(bad)).toThrow(RecordingError)
    const badEnv = baseRecording()
    ;(badEnv.runs as Record<string, unknown>[])[0].events = [{ id: 'e', at_s: 1, kind: 'k', title: 't', environment: 'moon', evidence: 'observed' }]
    expect(() => parseRecording(badEnv)).toThrow(RecordingError)
    const badSlo = baseRecording()
    ;(badSlo.runs as Record<string, unknown>[])[0].samples = [{ start_s: 0, end_s: 5, metrics: {}, slo_breached: 'yes' }]
    expect(() => parseRecording(badSlo)).toThrow(RecordingError)
    const badBounds = baseRecording()
    ;(badBounds.runs as Record<string, unknown>[])[0].samples = [{ start_s: 5, end_s: 5, metrics: {}, slo_breached: null }]
    expect(() => parseRecording(badBounds)).toThrow(RecordingError)
  })

  it('rejects invalid arm, source, and status', () => {
    for (const field of ['arm', 'source', 'status']) {
      const bad = baseRecording()
      ;(bad.runs as Record<string, unknown>[])[0][field] = 'bogus'
      expect(() => parseRecording(bad)).toThrow(RecordingError)
    }
  })

  it('rejects duplicate run and case ids and unknown case refs', () => {
    const dupRun = baseRecording()
    ;(dupRun.runs as unknown[]).push({ ...(dupRun.runs as object[])[0] })
    expect(() => parseRecording(dupRun)).toThrow(RecordingError)
    const dupCase = baseRecording()
    ;(dupCase.cases as unknown[]).push({ ...(dupCase.cases as object[])[0] })
    expect(() => parseRecording(dupCase)).toThrow(RecordingError)
    const orphan = baseRecording()
    ;(orphan.runs as Record<string, unknown>[])[0].case_id = 'case-missing'
    expect(() => parseRecording(orphan)).toThrow(RecordingError)
  })

  it('rejects out-of-range protocol durations and malformed counts', () => {
    const huge = baseRecording()
    ;(huge.protocol as Record<string, unknown>).horizon_s = 1e308
    expect(() => parseRecording(huge)).toThrow(RecordingError)
    const misaligned = baseRecording()
    ;(misaligned.protocol as Record<string, unknown>).horizon_s = 123
    expect(() => parseRecording(misaligned)).toThrow(RecordingError)
    const badWindow = baseRecording()
    ;(badWindow.protocol as Record<string, unknown>).window_s = 10
    expect(() => parseRecording(badWindow)).toThrow(RecordingError)
    const negative = baseRecording()
    ;((negative.runs as Record<string, unknown>[])[0].metrics as Record<string, unknown>).production_actions = -1
    expect(() => parseRecording(negative)).toThrow(RecordingError)
    const fractional = baseRecording()
    ;((fractional.runs as Record<string, unknown>[])[0].metrics as Record<string, unknown>).tokens = 3.5
    expect(() => parseRecording(fractional)).toThrow(RecordingError)
    const coverage = baseRecording()
    ;((coverage.runs as Record<string, unknown>[])[0].metrics as Record<string, unknown>).sample_coverage_pct = 140
    expect(() => parseRecording(coverage)).toThrow(RecordingError)
    const width = baseRecording()
    ;(width.runs as Record<string, unknown>[])[0].samples = [{ start_s: 0, end_s: 7, metrics: {}, slo_breached: null }]
    expect(() => parseRecording(width)).toThrow(RecordingError)
    const offgrid = baseRecording()
    ;(offgrid.runs as Record<string, unknown>[])[0].samples = [{ start_s: 1, end_s: 6, metrics: {}, slo_breached: null }]
    expect(() => parseRecording(offgrid)).toThrow(RecordingError)
    const badId = baseRecording()
    badId.id = 'recording-1'
    expect(() => parseRecording(badId)).toThrow(RecordingError)
    const negativeRps = baseRecording()
    ;(negativeRps.cases as Record<string, unknown>[])[0].workload_rps = 0
    expect(() => parseRecording(negativeRps)).toThrow(RecordingError)
  })

  it('rejects strings over 20 MB and non-JSON strings', () => {
    expect(() => parseRecording('{nope')).toThrow(RecordingError)
    expect(() => parseRecording(' '.repeat(20 * 1024 * 1024 + 1))).toThrow(RecordingError)
  })
})

describe('visibleRunEvents', () => {
  it('hides events after the cursor and keeps stable order at equal timestamps', () => {
    const early = visibleRunEvents(run(), 80)
    expect(early.map(event => event.id)).toEqual(['e1', 'e2'])
    const late = visibleRunEvents(run(), 90)
    expect(late.map(event => event.id)).toEqual(['e1', 'e2', 'e3'])
  })
})

describe('metricPoints', () => {
  it('emits recorded values and nulls for gaps and missing metrics', () => {
    const [times, qps] = metricPoints(run(), 'svc.gateway.qps', 30, 120)
    expect(times).toEqual([0, 5, 10, 15, 20, 25])
    expect(qps).toEqual([80, null, 81, null, null, null])
    const [, missing] = metricPoints(run(), 'db.query_p99_ms', 15, 120)
    expect(missing).toEqual([null, null, null])
  })

  it('only emits complete bins whose window finished by the cursor', () => {
    const [times, qps] = metricPoints(run(), 'svc.gateway.qps', 12, 120)
    expect(times).toEqual([0, 5])
    expect(qps).toEqual([80, null])
  })

  it('clips to the horizon and to t >= 0', () => {
    const [times] = metricPoints(run(), 'svc.gateway.qps', 500, 20)
    expect(times).toEqual([0, 5, 10, 15])
    expect(metricPoints(run(), 'svc.gateway.qps', -5, 120)).toEqual([[], []])
    expect(metricPoints(run(), 'svc.gateway.qps', 3, 120)).toEqual([[], []])
  })
})
