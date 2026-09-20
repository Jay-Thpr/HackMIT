import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'
import { AlertTriangle, ArrowRight, Database, FileUp, GitBranch, Layers3, Pause, Play, RotateCcw, Server, ShieldCheck } from 'lucide-react'
import {
  ARMS, ARM_LABELS, MAX_RECORDING_BYTES, metricPoints, parseRecording, RecordingError,
  visibleRunEvents, type Arm, type Case, type ComparisonEvent, type Recording, type Run,
} from '../comparison'

const METRICS: { key: string; label: string; unit: string }[] = [
  { key: 'svc.gateway.p99_ms', label: 'Gateway p99', unit: 'ms' },
  { key: 'svc.gateway.error_rate', label: 'Error rate', unit: '' },
  { key: 'svc.gateway.qps', label: 'Gateway load', unit: 'qps' },
  { key: 'svc.orders.retry_ratio', label: 'Retry ratio', unit: '' },
  { key: 'db.query_p99_ms', label: 'DB query p99', unit: 'ms' },
]
const STATUS_LABELS: Record<Run['status'], string> = {
  not_run: 'Not run', running: 'Running', completed: 'Completed',
  error: 'Error', timeout: 'Timed out', blocked: 'Blocked',
}

interface Listing { id: string; title: string | null; created_at: string | null; run_count: number }
interface Band { start: number; end: number }

function fmt(value: number | null | undefined, unit = ''): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return 'Not collected'
  const rendered = Math.abs(value) >= 100 ? Math.round(value).toLocaleString('en-US') : Number(value.toFixed(3)).toString()
  return unit ? `${rendered} ${unit}` : rendered
}

function secondsLabel(value: number): string {
  const m = Math.floor(value / 60)
  const s = Math.floor(value % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

function sourceBadge(run: Run): string {
  if (run.status === 'not_run') return 'Not run'
  if (run.source === 'synthetic') return 'Synthetic playback'
  return run.status === 'completed' || run.status === 'running' ? 'Live-recorded run' : 'Live attempt'
}

function actionBands(run: Run, cursor: number, horizon: number): Band[] {
  const visible = visibleRunEvents(run, cursor)
  return visible
    .filter(e => e.kind === 'action_start' && e.environment === 'production')
    .map(start => ({
      start: Math.max(0, start.at_s),
      end: Math.min(horizon, visible.find(e => e.kind === 'action_end' && e.action_id === start.action_id && e.at_s >= start.at_s)?.at_s ?? cursor),
    }))
}

function MetricChart({ times, values, maxY, horizon, label, bands }: { times: number[]; values: (number | null)[]; maxY: number; horizon: number; label: string; bands: Band[] }) {
  const container = useRef<HTMLDivElement>(null)
  const plot = useRef<uPlot | null>(null)
  const bandsRef = useRef(bands)
  bandsRef.current = bands
  const data = useMemo<uPlot.AlignedData>(() => [times, values], [times, values])
  useEffect(() => {
    const target = container.current
    if (!target) return
    const chart = new uPlot({
      width: Math.max(target.clientWidth, 160), height: 96,
      padding: [8, 8, 0, 0],
      legend: { show: false },
      cursor: { show: false },
      select: { show: false, left: 0, top: 0, width: 0, height: 0 },
      scales: { x: { time: false, range: [0, horizon] }, y: { auto: false, range: [0, Math.max(maxY * 1.1, 0.01)] } },
      axes: [
        { stroke: '#b9b2a6', font: '9px system-ui', grid: { show: false }, values: (_self, ticks) => ticks.map(tick => `${tick}s`), size: 18, ticks: { show: false } },
        { stroke: '#b9b2a6', font: '9px system-ui', grid: { stroke: '#efe9e0', width: 1 }, size: 34, ticks: { show: false } },
      ],
      series: [{}, { label, stroke: '#8a6f5c', width: 1.5, paths: uPlot.paths.stepped!({ align: 1 }), points: { show: false }, spanGaps: false }],
      hooks: { drawClear: [plot => {
        const ctx = plot.ctx
        for (const band of bandsRef.current) {
          const left = plot.valToPos(band.start, 'x', true)
          const right = plot.valToPos(band.end, 'x', true)
          ctx.save()
          ctx.fillStyle = 'rgba(154,132,103,0.10)'
          ctx.fillRect(left, plot.bbox.top, Math.max(right - left, 1), plot.bbox.height)
          ctx.strokeStyle = 'rgba(154,132,103,0.55)'
          ctx.lineWidth = 1
          for (const edge of [left, right]) {
            ctx.beginPath()
            ctx.moveTo(edge, plot.bbox.top)
            ctx.lineTo(edge, plot.bbox.top + plot.bbox.height)
            ctx.stroke()
          }
          ctx.restore()
        }
      }] },
    }, data, target)
    plot.current = chart
    const observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width
      if (width && width > 0) chart.setSize({ width, height: 96 })
    })
    observer.observe(target)
    return () => { observer.disconnect(); chart.destroy(); plot.current = null }
  }, [horizon, maxY, label])
  useEffect(() => { plot.current?.setData(data); plot.current?.redraw() }, [data])
  return <div className="comparison-chart" role="img" aria-label={`${label} over elapsed seconds; blank regions mean not collected`} ref={container} />
}

function EventList({ run, cursor, selected, onSelect }: { run: Run; cursor: number; selected: string | null; onSelect: (event: ComparisonEvent) => void }) {
  const visible = visibleRunEvents(run, cursor)
  const production = visible.filter(event => event.environment !== 'clone')
  const clones = visible.filter(event => event.environment === 'clone')
  const render = (event: ComparisonEvent) => (
    <li key={event.id}>
      <button className={selected === event.id ? 'selected' : ''} onClick={() => onSelect(event)}>
        <span className="comparison-event-time mono">{secondsLabel(event.at_s)}</span>
        <span className="comparison-event-body">
          <strong>{event.title}</strong>
          <span className="comparison-event-badges">
            <i data-evidence={event.evidence}>{event.evidence}</i>
            <i data-environment={event.environment}>{event.environment}</i>
            <em>{event.kind}</em>
          </span>
        </span>
      </button>
    </li>
  )
  return <div className="comparison-events">
    {visible.length === 0 && <p className="comparison-empty-note">{run.status === 'not_run' ? 'Not run.' : 'No events recorded at this point in the replay.'}</p>}
    {production.length > 0 && <><span className="comparison-event-group">Production &amp; observer</span><ul>{production.map(render)}</ul></>}
    {clones.length > 0 && <><span className="comparison-event-group">Clone environment</span><ul>{clones.map(render)}</ul></>}
  </div>
}

function OutcomeCard({ arm, run, reveal }: { arm: Arm; run: Run | null; reveal: boolean }) {
  const metrics = run?.metrics ?? null
  return <section className="comparison-outcome panel" data-arm={arm}>
    <span className="overline">{ARM_LABELS[arm]}</span>
    {!reveal ? <p className="comparison-outcome-hidden">Final results hidden until the replay ends or “Show final results” is enabled.</p>
      : !run ? <p className="comparison-outcome-hidden">No run recorded for this responder.</p>
      : <dl>
          <div><dt>Final diagnosis</dt><dd>{metrics?.diagnosis ?? 'Not collected'}</dd></div>
          <div><dt>Recovery</dt><dd>{metrics ? (metrics.recovery_status === 'not_applicable' ? 'n/a' : metrics.recovery_s !== null ? fmt(metrics.recovery_s, 's') : metrics.recovery_status === 'unknown' ? 'Not collected' : metrics.recovery_status.replaceAll('_', ' ')) : 'Not collected'}</dd></div>
          <div><dt>Failed checkouts (est.)</dt><dd>{metrics ? fmt(metrics.failed_checkouts_estimate) : 'Not collected'}</dd></div>
        </dl>}
  </section>
}

export function ComparisonReplay(): React.JSX.Element {
  const [listings, setListings] = useState<Listing[]>([])
  const [listError, setListError] = useState<string | null>(null)
  const [recording, setRecording] = useState<Recording | null>(null)
  const [imported, setImported] = useState<Recording[]>([])
  const [loadError, setLoadError] = useState<string | null>(null)
  const [caseId, setCaseId] = useState<string | null>(null)
  const [armA, setArmA] = useState<Arm>('observe')
  const [armB, setArmB] = useState<Arm>('probe')
  const [cursor, setCursor] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(1)
  const [showFinal, setShowFinal] = useState(false)
  const [selectedEvent, setSelectedEvent] = useState<ComparisonEvent | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let cancelled = false
    fetch('/api/comparisons')
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`status ${res.status}`)))
      .then((rows: Listing[]) => { if (!cancelled) setListings(rows) })
      .catch(() => { if (!cancelled) setListError('The comparison API is not reachable. Saved recordings could not be listed.') })
    return () => { cancelled = true }
  }, [])

  const adopt = useCallback((next: Recording, local: boolean) => {
    setRecording(next)
    if (local) setImported(items => items.some(item => item.id === next.id) ? items : [...items, next])
    setCaseId(next.cases[0]?.id ?? null)
    setCursor(0)
    setPlaying(false)
    setShowFinal(false)
    setSelectedEvent(null)
    setLoadError(null)
  }, [])

  const openRecording = useCallback((id: string) => {
    const local = imported.find(item => item.id === id)
    if (local) { adopt(local, false); return }
    fetch(`/api/comparisons/${encodeURIComponent(id)}`)
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`status ${res.status}`)))
      .then(json => adopt(parseRecording(json), false))
      .catch(() => setLoadError(`Recording ${id} could not be loaded.`))
  }, [adopt, imported])

  const loadExample = useCallback(() => {
    fetch('/cmp-example.json')
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`status ${res.status}`)))
      .then(json => adopt(parseRecording(json), true))
      .catch(() => setLoadError('The bundled illustrative example could not be loaded.'))
  }, [adopt])

  const onFile = useCallback((file: File | undefined) => {
    if (!file) return
    if (file.size > MAX_RECORDING_BYTES) { setLoadError('Files larger than 20 MB are not accepted.'); return }
    file.text()
      .then(raw => adopt(parseRecording(raw), true))
      .catch(error => setLoadError(error instanceof RecordingError ? `Not a valid recording: ${error.message}` : 'That file is not valid JSON.'))
  }, [adopt])

  const selectedCase: Case | null = recording?.cases.find(item => item.id === caseId) ?? recording?.cases[0] ?? null
  const caseRuns = useMemo(() => {
    const byArm = new Map<Arm, Run>()
    if (recording && selectedCase) {
      for (const run of recording.runs) if (run.case_id === selectedCase.id) byArm.set(run.arm, run)
    }
    return byArm
  }, [recording, selectedCase])
  const runA = caseRuns.get(armA) ?? null
  const runB = caseRuns.get(armB) ?? null

  const horizon = recording?.protocol.horizon_s ?? 300
  const duration = useMemo(() => {
    if (!recording || !selectedCase) return 0
    const ends = recording.runs.filter(run => run.case_id === selectedCase.id).map(run => run.duration_s)
    return Math.max(horizon, ...ends, 0)
  }, [recording, selectedCase, horizon])
  const finished = duration > 0 && cursor >= duration
  const revealFinal = showFinal || finished

  useEffect(() => {
    if (selectedEvent && selectedEvent.at_s > cursor) setSelectedEvent(null)
  }, [cursor, selectedEvent])

  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    const id = window.setInterval(() => {
      const now = performance.now()
      const delta = Math.min((now - last) / 1000, 0.5)
      last = now
      setCursor(current => {
        const next = Math.min(duration, current + delta * speed)
        if (next >= duration) setPlaying(false)
        return next
      })
    }, 100)
    return () => window.clearInterval(id)
  }, [playing, speed, duration])

  const sharedMax = useMemo(() => {
    const out: Record<string, number> = {}
    for (const metric of METRICS) {
      let peak = 0
      for (const run of [runA, runB]) {
        if (!run) continue
        const [, values] = metricPoints(run, metric.key, cursor, horizon)
        for (const value of values) if (value !== null && value > peak) peak = value
      }
      out[metric.key] = peak
    }
    return out
  }, [runA, runB, cursor, horizon])

  const chooseCase = useCallback((id: string) => {
    setCaseId(id)
    setCursor(0)
    setPlaying(false)
    setShowFinal(false)
    setSelectedEvent(null)
  }, [])

  const chooseArm = useCallback((slot: 'A' | 'B', arm: Arm) => {
    (slot === 'A' ? setArmA : setArmB)(arm)
    setSelectedEvent(null)
  }, [])

  const synthetic = recording !== null && recording.runs.some(run => run.source === 'synthetic')
  const anyLive = recording !== null && recording.runs.some(run => run.source === 'live')
  const provenanceLines = recording ? [...caseRuns.values()].flatMap(run => [
    ...Object.entries(run.provenance).map(([key, value]) => `${run.arm}: ${key}=${value}`),
    ...run.warnings.map(warning => `${run.arm}: warning — ${warning}`),
  ]) : []

  const armPanel = (arm: Arm, run: Run | null, slot: 'A' | 'B') => (
    <section className="comparison-panel panel" key={slot}>
      <div className="comparison-panel-head">
        <div>
          <span className="overline">RESPONDER {slot}</span>
          <h3>{ARM_LABELS[arm]}</h3>
        </div>
        <select aria-label={`Responder ${slot}`} value={arm} onChange={event => chooseArm(slot, event.target.value as Arm)}>
          {ARMS.map(item => <option key={item} value={item}>{ARM_LABELS[item]}</option>)}
        </select>
      </div>
      <div className="comparison-run-meta">
        {run ? <>
          <span className="quiet-badge" data-source={run.source}><i />{sourceBadge(run)}</span>
          <span className="quiet-badge" data-status={run.status}>{STATUS_LABELS[run.status]}</span>
          {run.warnings.map((warning, i) => <span key={i} className="comparison-warning"><AlertTriangle size={12} />{warning}</span>)}
        </> : <span className="quiet-badge"><i />No run recorded for this responder</span>}
      </div>
      {run && run.status !== 'not_run' ? <>
        <div className="comparison-charts">
          {METRICS.map(metric => {
            const [times, values] = metricPoints(run, metric.key, cursor, horizon)
            return <div key={metric.key} className="comparison-chart-cell">
              <div className="comparison-chart-title"><span>{metric.label}{metric.unit ? ` · ${metric.unit}` : ''}</span><strong>{fmt(values.at(-1) ?? null, metric.unit)}</strong></div>
              <MetricChart times={times} values={values} maxY={sharedMax[metric.key] ?? 0} horizon={horizon} label={metric.label} bands={actionBands(run, cursor, horizon)} />
            </div>
          })}
        </div>
        <p className="comparison-note comparison-band-note">Shading: applied production actions through recorded release (or current cursor).</p>
        <EventList run={run} cursor={cursor} selected={selectedEvent?.id ?? null} onSelect={setSelectedEvent} />
      </> : <p className="comparison-empty-note">{run ? 'This responder did not run for the selected case. No data is claimed.' : 'No recording exists for this responder and case.'}</p>}
    </section>
  )

  return <div className="comparison-view">
    <section className="workspace-header">
      <div>
        <div className="workspace-brief"><span className="case-id">COMPARISON</span><span className="case-state"><i />Recorded replays</span></div>
        <h1>Compare responders</h1>
        <p>Saved run recordings played back side by side. Development-set comparison, not a held-out benchmark.</p>
      </div>
      <div className="workspace-header-actions">
        <span className="quiet-badge"><Server size={12} />Datadog · Not connected — deferred</span>
      </div>
    </section>

    <section className="panel comparison-picker">
      <div className="comparison-picker-row">
        <label htmlFor="comparison-recording">Recording</label>
        <select id="comparison-recording" value={recording?.id ?? ''} onChange={event => event.target.value && openRecording(event.target.value)}>
          <option value="" disabled>{listings.length || imported.length ? 'Choose a recording' : 'No recordings yet'}</option>
          {listings.map(item => <option key={item.id} value={item.id}>{item.title ?? item.id} · {item.run_count} runs</option>)}
          {imported.filter(item => !listings.some(row => row.id === item.id)).map(item => <option key={item.id} value={item.id}>{item.title} · imported file</option>)}
        </select>
        <button className="secondary-button" onClick={() => fileInput.current?.click()}><FileUp size={14} />Open a recording file</button>
        <input ref={fileInput} type="file" accept="application/json,.json" hidden onChange={event => { onFile(event.target.files?.[0]); event.target.value = '' }} />
      </div>
      {listError && <p className="comparison-note">{listError} You can still open a saved recording file or the bundled example.</p>}
      {loadError && <p className="comparison-note" role="alert">{loadError}</p>}
      {!recording && <div className="comparison-setup">
        <h3>No comparison recording loaded</h3>
        <p>Comparison recordings are saved <code>cmp-*.json</code> files produced by the bench harness. When the read-only API lists them above, pick one to replay two responders side by side — charts, agent events, and supplied final scores. Nothing here runs an agent or acts on infrastructure.</p>
        <p>To explore the interface without a saved run, load the bundled illustrative playback. It is synthetic: no vendor was run and no comparative result is implied.</p>
        <button className="primary-button" onClick={loadExample}>Load illustrative example</button>
      </div>}
    </section>

    {recording && <>
      {synthetic && <div className="comparison-banner" role="status"><AlertTriangle size={14} /><strong>Synthetic playback.</strong><span>Some or all runs in this recording are marked synthetic — not measured results, and no vendor comparison is implied.</span></div>}
      {!synthetic && !anyLive && <div className="comparison-banner" role="status"><AlertTriangle size={14} /><strong>No live-recorded runs</strong><span>are present in this recording.</span></div>}

      <section className="panel comparison-controls">
        <div className="comparison-picker-row">
          <label htmlFor="comparison-case">Case</label>
          <select id="comparison-case" value={selectedCase?.id ?? ''} onChange={event => chooseCase(event.target.value)}>
            {recording.cases.map(item => <option key={item.id} value={item.id}>{revealFinal ? item.label : item.id}</option>)}
          </select>
          <span className="comparison-case-meta">{recording.title}{revealFinal && selectedCase ? ` · expected: ${selectedCase.expected}` : ''}</span>
        </div>
        <div className="comparison-topology" aria-label="Sandbox topology">
          <span className="overline">SANDBOX TOPOLOGY</span>
          <div className="comparison-topology-flow">
            <span><Layers3 size={13} />Gateway</span><ArrowRight size={12} /><span>Orders</span><ArrowRight size={12} /><span>Payments</span><ArrowRight size={12} /><span><Database size={13} />DB</span>
          </div>
        </div>
        <div className="comparison-scrubber">
          <button className="icon-button" aria-label={playing ? 'Pause replay' : 'Play replay'} onClick={() => { if (!playing && cursor >= duration) setCursor(0); setPlaying(!playing) }}>{playing ? <Pause size={15} /> : <Play size={15} />}</button>
          <button className="icon-button" aria-label="Reset replay" onClick={() => { setCursor(0); setPlaying(false); setSelectedEvent(null) }}><RotateCcw size={15} /></button>
          {[1, 2, 4].map(value => <button key={value} className={`comparison-speed ${speed === value ? 'active' : ''}`} aria-label={`Playback speed ${value} times`} onClick={() => setSpeed(value)}>{value}×</button>)}
          <input type="range" aria-label="Comparison timeline" min={0} max={duration} step={1} value={Math.floor(cursor)} onChange={event => { setCursor(Number(event.target.value)); setPlaying(false) }} />
          <span className="mono comparison-clock">{secondsLabel(cursor)} / {secondsLabel(duration)}</span>
          <label className="comparison-final-toggle"><input type="checkbox" checked={showFinal} onChange={event => setShowFinal(event.target.checked)} />Show final results</label>
        </div>
        <p className="comparison-note">Charts cover the {horizon}s measurement window; the timeline continues through recorded cleanup. Blank chart regions mean a value was not collected — never zero.</p>
      </section>

      <div className="comparison-outcomes">
        <OutcomeCard arm={armA} run={runA} reveal={revealFinal} />
        <OutcomeCard arm={armB} run={runB} reveal={revealFinal} />
      </div>

      {selectedEvent && <section className="panel comparison-event-detail" role="status">
        <div className="panel-title"><div><span className="overline">EVENT DETAIL · {selectedEvent.kind}</span><h3>{selectedEvent.title}</h3></div><button className="icon-button" aria-label="Close event detail" onClick={() => setSelectedEvent(null)}>×</button></div>
        <p>{selectedEvent.detail || 'No detail was recorded for this event.'}</p>
        <div className="comparison-event-facts">
          <span>at {secondsLabel(selectedEvent.at_s)}</span>
          <span>environment: {selectedEvent.environment}</span>
          <span>evidence: {selectedEvent.evidence}</span>
          {selectedEvent.diagnosis && <span>diagnosis: {selectedEvent.diagnosis}</span>}
          {selectedEvent.action_id && <span>action: {selectedEvent.action_id}</span>}
          {selectedEvent.ttl_s !== null && <span>TTL: {selectedEvent.ttl_s}s</span>}
          {selectedEvent.reference && <span>ref: {selectedEvent.reference}</span>}
        </div>
      </section>}

      <div className="comparison-grid">
        {armPanel(armA, runA, 'A')}
        {armPanel(armB, runB, 'B')}
      </div>

      <section className="panel comparison-summary">
        <div className="panel-title"><div><span className="overline">SUPPLIED RESULTS · ALL RESPONDERS</span><h3>Summary for {revealFinal ? selectedCase?.label : selectedCase?.id}</h3></div><GitBranch size={16} /></div>
        {!revealFinal && <p className="comparison-note">Final metrics and diagnoses stay hidden until the replay ends or you choose “Show final results”. Run status and errors are always shown.</p>}
        <table>
          <thead><tr><th>Responder</th><th>Source</th><th>Status</th>{revealFinal && <><th>Detection</th><th>Diagnosis</th><th>Correct</th><th>Correct diagnosis at</th><th>Recovery</th><th>Failed checkouts (est.)</th><th>Successful checkouts (est.)</th><th>Prod. actions</th><th>Clone actions</th><th>Rollback failures</th><th>Coverage</th><th>Tokens</th><th>Cost (USD)</th></>}</tr></thead>
          <tbody>
            {ARMS.map(arm => {
              const run = caseRuns.get(arm)
              const metrics = run?.metrics ?? null
              return <tr key={arm} data-arm={arm}>
                <td>{ARM_LABELS[arm]}</td>
                <td>{run ? (run.source === 'live' ? 'Live-recorded' : 'Synthetic') : 'Not collected'}</td>
                <td data-status={run?.status ?? 'missing'}>{run ? STATUS_LABELS[run.status] : 'Not collected'}</td>
                {revealFinal && <>
                  <td>{metrics ? fmt(metrics.detection_s, 's') : 'Not collected'}</td>
                  <td>{metrics?.diagnosis ?? 'Not collected'}</td>
                  <td>{metrics ? (metrics.correct === null ? 'Not collected' : metrics.correct ? 'Yes' : 'No') : 'Not collected'}</td>
                  <td>{metrics ? fmt(metrics.first_correct_s, 's') : 'Not collected'}</td>
                  <td>{metrics ? (metrics.recovery_status === 'not_applicable' ? 'n/a' : metrics.recovery_s !== null ? fmt(metrics.recovery_s, 's') : metrics.recovery_status === 'unknown' ? 'Not collected' : metrics.recovery_status.replaceAll('_', ' ')) : 'Not collected'}</td>
                  <td>{metrics ? fmt(metrics.failed_checkouts_estimate) : 'Not collected'}</td>
                  <td>{metrics ? fmt(metrics.successful_checkouts_estimate) : 'Not collected'}</td>
                  <td>{metrics ? String(metrics.production_actions) : 'Not collected'}</td>
                  <td>{metrics ? String(metrics.clone_actions) : 'Not collected'}</td>
                  <td>{metrics ? String(metrics.rollback_failures) : 'Not collected'}</td>
                  <td>{metrics ? `${fmt(metrics.sample_coverage_pct)}%` : 'Not collected'}</td>
                  <td>{metrics ? fmt(metrics.tokens) : 'Not collected'}</td>
                  <td>{metrics ? fmt(metrics.cost_usd) : 'Not collected'}</td>
                </>}
              </tr>
            })}
          </tbody>
        </table>
        <div className="comparison-provenance">
          <details>
            <summary>Provenance, warnings &amp; protocol notes ({provenanceLines.length + recording.protocol.notes.length})</summary>
            <ul>
              {provenanceLines.map((line, i) => <li key={i} className="mono">{line}</li>)}
              {recording.protocol.notes.map((note, i) => <li key={`n${i}`}>{note}</li>)}
            </ul>
          </details>
          <p className="comparison-note"><ShieldCheck size={12} />Development-set comparison, not a held-out benchmark. Playback never reruns infrastructure actions.</p>
        </div>
      </section>
    </>}
  </div>
}
