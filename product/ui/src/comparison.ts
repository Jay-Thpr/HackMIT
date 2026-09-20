export type Arm = 'elastic' | 'observe' | 'probe' | 'clone_probe'
export const ARMS: Arm[] = ['elastic', 'observe', 'probe', 'clone_probe']
export const ARM_LABELS: Record<Arm, string> = {
  elastic: 'Elastic Agent Builder · read-only RCA',
  observe: 'Faultline · Observe',
  probe: 'Faultline · Probe',
  clone_probe: 'Faultline · Clone + Probe',
}
export const SCHEMA_VERSION = 'faultline-comparison/1'
export const MAX_RECORDING_BYTES = 20 * 1024 * 1024

export interface Protocol {
  name: string
  window_s: number
  baseline_s: number
  horizon_s: number
  detection_s: number
  recovery_s: number
  review_s: number
  action_budget: number
  model: string
  clone_budget: number
  notes: string[]
}

export interface Case {
  id: string
  label: string
  world: 'storm' | 'degraded' | 'healthy'
  expected: string
  workload_rps: number
  params: Record<string, number>
}

export interface Sample {
  start_s: number
  end_s: number
  metrics: Record<string, number>
  slo_breached: boolean | null
}

export interface ComparisonEvent {
  id: string
  at_s: number
  kind: string
  title: string
  detail: string
  environment: 'production' | 'clone' | 'observer'
  evidence: 'observed' | 'inferred' | 'measured' | 'operator'
  reference: string | null
  diagnosis: string | null
  action_id: string | null
  ttl_s: number | null
}

export interface Metrics {
  diagnosis: string | null
  correct: boolean | null
  detection_s: number | null
  first_correct_s: number | null
  recovery_s: number | null
  recovery_status: 'recovered' | 'not_recovered' | 'not_applicable' | 'unknown'
  failed_checkouts_estimate: number | null
  successful_checkouts_estimate: number | null
  sample_coverage_pct: number
  production_actions: number
  clone_actions: number
  rollback_failures: number
  tokens: number | null
  cost_usd: number | null
}

export interface Run {
  id: string
  case_id: string
  arm: Arm
  source: 'live' | 'synthetic'
  status: 'not_run' | 'running' | 'completed' | 'error' | 'timeout' | 'blocked'
  started_at: string | null
  duration_s: number
  samples: Sample[]
  events: ComparisonEvent[]
  metrics: Metrics | null
  provenance: Record<string, string>
  warnings: string[]
}

export interface Recording {
  schema_version: typeof SCHEMA_VERSION
  id: string
  title: string
  created_at: string
  protocol: Protocol
  cases: Case[]
  runs: Run[]
}

const DEFAULT_PROTOCOL: Protocol = {
  name: 'Development-set comparison; diagnosis and reversible mitigation only',
  window_s: 5, baseline_s: 130, horizon_s: 300, detection_s: 60, recovery_s: 60, review_s: 30,
  action_budget: 5, model: 'gpt-4.1', clone_budget: 3, notes: [],
}
const ID_PATTERN = /^cmp-[A-Za-z0-9_-]{1,100}$/
const WORLDS = new Set(['storm', 'degraded', 'healthy'])
const ENVIRONMENTS = new Set(['production', 'clone', 'observer'])
const EVIDENCE = new Set(['observed', 'inferred', 'measured', 'operator'])
const SOURCES = new Set(['live', 'synthetic'])
const STATUSES = new Set(['not_run', 'running', 'completed', 'error', 'timeout', 'blocked'])
const RECOVERY = new Set(['recovered', 'not_recovered', 'not_applicable', 'unknown'])

export class RecordingError extends Error {}

function fail(reason: string): never {
  throw new RecordingError(reason)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function finite(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) fail(`${field} must be a finite number`)
  return value
}

function bounded(value: unknown, field: string, min: number, max: number, integer = true): number {
  const out = finite(value, field)
  if (integer && !Number.isInteger(out)) fail(`${field} must be an integer`)
  if (out < min || out > max) fail(`${field} must be between ${min} and ${max}`)
  return out
}

function text(value: unknown, field: string): string {
  if (typeof value !== 'string') fail(`${field} must be a string`)
  return value
}

function optionalText(value: unknown, field: string): string | null {
  if (value === null || value === undefined) return null
  return text(value, field)
}

function optionalNumber(value: unknown, field: string): number | null {
  if (value === null || value === undefined) return null
  return finite(value, field)
}

function duration(value: unknown, field: string, fallback: number, min: number, max: number): number {
  if (value === undefined) return fallback
  const out = bounded(value, field, min, max)
  if (out % 5 !== 0) fail(`${field} must align to 5 s windows`)
  return out
}

function parseProtocol(value: unknown): Protocol {
  if (value === undefined) return { ...DEFAULT_PROTOCOL }
  if (!isRecord(value)) fail('protocol must be an object')
  const window_s = value.window_s === undefined ? DEFAULT_PROTOCOL.window_s : finite(value.window_s, 'protocol.window_s')
  if (window_s !== 5) fail('protocol.window_s must be 5')
  return {
    name: value.name === undefined ? DEFAULT_PROTOCOL.name : text(value.name, 'protocol.name'),
    window_s,
    baseline_s: duration(value.baseline_s, 'protocol.baseline_s', DEFAULT_PROTOCOL.baseline_s, 120, 300),
    horizon_s: duration(value.horizon_s, 'protocol.horizon_s', DEFAULT_PROTOCOL.horizon_s, 120, 900),
    detection_s: duration(value.detection_s, 'protocol.detection_s', DEFAULT_PROTOCOL.detection_s, 5, 120),
    recovery_s: duration(value.recovery_s, 'protocol.recovery_s', DEFAULT_PROTOCOL.recovery_s, 5, 120),
    review_s: duration(value.review_s, 'protocol.review_s', DEFAULT_PROTOCOL.review_s, 15, 120),
    action_budget: value.action_budget === undefined ? DEFAULT_PROTOCOL.action_budget : bounded(value.action_budget, 'protocol.action_budget', 1, 5),
    model: value.model === undefined ? DEFAULT_PROTOCOL.model : text(value.model, 'protocol.model'),
    clone_budget: value.clone_budget === undefined ? DEFAULT_PROTOCOL.clone_budget : bounded(value.clone_budget, 'protocol.clone_budget', 1, 5),
    notes: value.notes === undefined ? [] : (Array.isArray(value.notes) ? value.notes.map((note, i) => text(note, `protocol.notes[${i}]`)) : fail('protocol.notes must be a list')),
  }
}

function parseCase(value: unknown, index: number): Case {
  if (!isRecord(value)) fail(`cases[${index}] must be an object`)
  if (!WORLDS.has(value.world as string)) fail(`cases[${index}].world is unsupported`)
  const params = value.params === undefined ? {} : value.params
  if (!isRecord(params)) fail(`cases[${index}].params must be an object`)
  const out: Record<string, number> = {}
  for (const [key, entry] of Object.entries(params)) out[key] = finite(entry, `cases[${index}].params.${key}`)
  return {
    id: text(value.id, `cases[${index}].id`),
    label: text(value.label, `cases[${index}].label`),
    world: value.world as Case['world'],
    expected: text(value.expected, `cases[${index}].expected`),
    workload_rps: value.workload_rps === undefined ? 80 : bounded(value.workload_rps, `cases[${index}].workload_rps`, 1, Number.MAX_SAFE_INTEGER),
    params: out,
  }
}

function parseSample(value: unknown, field: string): Sample {
  if (!isRecord(value)) fail(`${field} must be an object`)
  const start = finite(value.start_s, `${field}.start_s`)
  const end = finite(value.end_s, `${field}.end_s`)
  if (end <= start) fail(`${field} boundaries must increase`)
  if (Math.abs(end - start - 5) > 0.01) fail(`${field} must span exactly one 5 s window`)
  if (Math.abs(start % 5) > 0.01 || Math.abs(end % 5) > 0.01) fail(`${field} must align to 5 s windows`)
  const rawMetrics = value.metrics === undefined ? {} : value.metrics
  if (!isRecord(rawMetrics)) fail(`${field}.metrics must be an object`)
  const metrics: Record<string, number> = {}
  for (const [key, entry] of Object.entries(rawMetrics)) metrics[key] = finite(entry, `${field}.metrics.${key}`)
  const slo = value.slo_breached
  if (slo !== null && slo !== undefined && typeof slo !== 'boolean') fail(`${field}.slo_breached must be boolean or null`)
  return { start_s: start, end_s: end, metrics, slo_breached: slo ?? null }
}

function parseEvent(value: unknown, field: string): ComparisonEvent {
  if (!isRecord(value)) fail(`${field} must be an object`)
  if (!ENVIRONMENTS.has(value.environment as string)) fail(`${field}.environment is unsupported`)
  if (!EVIDENCE.has(value.evidence as string)) fail(`${field}.evidence is unsupported`)
  return {
    id: text(value.id, `${field}.id`),
    at_s: finite(value.at_s, `${field}.at_s`),
    kind: text(value.kind, `${field}.kind`),
    title: text(value.title, `${field}.title`),
    detail: value.detail === undefined ? '' : text(value.detail, `${field}.detail`),
    environment: value.environment as ComparisonEvent['environment'],
    evidence: value.evidence as ComparisonEvent['evidence'],
    reference: optionalText(value.reference, `${field}.reference`),
    diagnosis: optionalText(value.diagnosis, `${field}.diagnosis`),
    action_id: optionalText(value.action_id, `${field}.action_id`),
    ttl_s: optionalNumber(value.ttl_s, `${field}.ttl_s`),
  }
}

function parseMetrics(value: unknown, field: string): Metrics | null {
  if (value === null || value === undefined) return null
  if (!isRecord(value)) fail(`${field} must be an object`)
  const recovery = value.recovery_status === undefined ? 'unknown' : value.recovery_status
  if (!RECOVERY.has(recovery as string)) fail(`${field}.recovery_status is unsupported`)
  const correct = value.correct
  if (correct !== null && correct !== undefined && typeof correct !== 'boolean') fail(`${field}.correct must be boolean or null`)
  return {
    diagnosis: optionalText(value.diagnosis, `${field}.diagnosis`),
    correct: correct ?? null,
    detection_s: optionalNumber(value.detection_s, `${field}.detection_s`),
    first_correct_s: optionalNumber(value.first_correct_s, `${field}.first_correct_s`),
    recovery_s: optionalNumber(value.recovery_s, `${field}.recovery_s`),
    recovery_status: recovery as Metrics['recovery_status'],
    failed_checkouts_estimate: optionalNumber(value.failed_checkouts_estimate, `${field}.failed_checkouts_estimate`),
    successful_checkouts_estimate: optionalNumber(value.successful_checkouts_estimate, `${field}.successful_checkouts_estimate`),
    sample_coverage_pct: value.sample_coverage_pct === undefined ? 0 : bounded(value.sample_coverage_pct, `${field}.sample_coverage_pct`, 0, 100, false),
    production_actions: value.production_actions === undefined ? 0 : bounded(value.production_actions, `${field}.production_actions`, 0, Number.MAX_SAFE_INTEGER),
    clone_actions: value.clone_actions === undefined ? 0 : bounded(value.clone_actions, `${field}.clone_actions`, 0, Number.MAX_SAFE_INTEGER),
    rollback_failures: value.rollback_failures === undefined ? 0 : bounded(value.rollback_failures, `${field}.rollback_failures`, 0, Number.MAX_SAFE_INTEGER),
    tokens: value.tokens === null || value.tokens === undefined ? null : bounded(value.tokens, `${field}.tokens`, 0, Number.MAX_SAFE_INTEGER),
    cost_usd: optionalNumber(value.cost_usd, `${field}.cost_usd`),
  }
}

function parseRun(value: unknown, index: number): Run {
  const field = `runs[${index}]`
  if (!isRecord(value)) fail(`${field} must be an object`)
  if (!ARMS.includes(value.arm as Arm)) fail(`${field}.arm is unsupported`)
  if (!SOURCES.has(value.source as string)) fail(`${field}.source is unsupported`)
  if (!STATUSES.has(value.status as string)) fail(`${field}.status is unsupported`)
  if (!Array.isArray(value.samples)) fail(`${field}.samples must be a list`)
  if (!Array.isArray(value.events)) fail(`${field}.events must be a list`)
  const provenance = value.provenance === undefined ? {} : value.provenance
  if (!isRecord(provenance)) fail(`${field}.provenance must be an object`)
  const warnings = value.warnings === undefined ? [] : value.warnings
  if (!Array.isArray(warnings)) fail(`${field}.warnings must be a list`)
  return {
    id: text(value.id, `${field}.id`),
    case_id: text(value.case_id, `${field}.case_id`),
    arm: value.arm as Arm,
    source: value.source as Run['source'],
    status: value.status as Run['status'],
    started_at: optionalText(value.started_at, `${field}.started_at`),
    duration_s: value.duration_s === undefined ? 0 : bounded(value.duration_s, `${field}.duration_s`, 0, 86400, false),
    samples: value.samples.map((sample, i) => parseSample(sample, `${field}.samples[${i}]`)),
    events: value.events.map((event, i) => parseEvent(event, `${field}.events[${i}]`)),
    metrics: parseMetrics(value.metrics, `${field}.metrics`),
    provenance: Object.fromEntries(Object.entries(provenance).map(([key, entry]) => [key, text(entry, `${field}.provenance.${key}`)])),
    warnings: warnings.map((warning, i) => text(warning, `${field}.warnings[${i}]`)),
  }
}

export function parseRecording(value: unknown): Recording {
  if (typeof value === 'string') {
    if (value.length > MAX_RECORDING_BYTES) fail('recording exceeds 20 MB')
    try {
      value = JSON.parse(value)
    } catch {
      fail('recording is not valid JSON')
    }
  }
  if (!isRecord(value)) fail('recording must be an object')
  if (value.schema_version !== SCHEMA_VERSION) fail(`unsupported schema_version`)
  if (typeof value.id !== 'string' || !ID_PATTERN.test(value.id)) fail('id must match cmp-*')
  if (!Array.isArray(value.cases) || value.cases.length === 0) fail('cases must be a non-empty list')
  if (!Array.isArray(value.runs)) fail('runs must be a list')
  const cases = value.cases.map(parseCase)
  const runs = value.runs.map(parseRun)
  if (new Set(cases.map(item => item.id)).size !== cases.length) fail('duplicate case id')
  if (new Set(runs.map(item => item.id)).size !== runs.length) fail('duplicate run id')
  const caseIds = new Set(cases.map(item => item.id))
  for (const run of runs) if (!caseIds.has(run.case_id)) fail(`run ${run.id} references unknown case ${run.case_id}`)
  return {
    schema_version: SCHEMA_VERSION,
    id: text(value.id, 'id'),
    title: value.title === undefined ? 'Faultline comparison lab' : text(value.title, 'title'),
    created_at: value.created_at === undefined ? '' : text(value.created_at, 'created_at'),
    protocol: parseProtocol(value.protocol),
    cases,
    runs,
  }
}

export function visibleRunEvents(run: Run, cursor: number): ComparisonEvent[] {
  return run.events
    .map((event, index) => ({ event, index }))
    .filter(({ event }) => event.at_s >= 0 && event.at_s <= cursor)
    .sort((a, b) => (a.event.at_s - b.event.at_s) || (a.index - b.index))
    .map(({ event }) => event)
}

export function metricPoints(run: Run, metric: string, cursor: number, horizon: number): [number[], (number | null)[]] {
  const times: number[] = []
  const values: (number | null)[] = []
  const window_s = 5
  const last = Math.min(cursor, horizon)
  for (let t = 0; t + window_s <= last; t += window_s) {
    times.push(t)
    const sample = run.samples.find(item =>
      Math.abs(item.start_s - t) <= 0.01 && Math.abs(item.end_s - (t + window_s)) <= 0.01 && item.end_s <= cursor)
    const value = sample?.metrics[metric]
    values.push(typeof value === 'number' && Number.isFinite(value) ? value : null)
  }
  return [times, values]
}

export { ComparisonReplay } from './components/ComparisonReplay'
