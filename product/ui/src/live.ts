import type { Scenario } from './model'
import { useWorkspace } from './store'

/** Load real incidents from the Product read-only API (`faultline ui`, /api/*) into the store.
 *  The synthetic examples remain available and stay selected by default; live incidents are listed
 *  first. `?live` selects the newest real incident, `?incident=<id>` a specific one. Any failure
 *  (no API, offline) leaves the workspace on the synthetic examples. */
export function requestedIncident(search = typeof location === 'undefined' ? '' : location.search): string | true | null {
  const params = new URLSearchParams(search)
  const id = params.get('incident')
  if (id) return id
  return params.has('live') ? true : null
}

export async function loadLiveScenarios(base = '/api'): Promise<Scenario[]> {
  try {
    const index = await fetch(`${base}/incidents`)
    if (!index.ok) return []
    const incidents: { id: string }[] = await index.json()
    const wanted = requestedIncident()
    const ids = incidents.map(item => item.id)
    // `?incident=<id>` may name an incident that has not written its first audit event yet
    // (watch is still waiting for the breach): follow it anyway and let the stream fill it in.
    if (typeof wanted === 'string' && !ids.includes(wanted)) ids.unshift(wanted)
    const loaded = await Promise.all(ids.slice(0, 12).map(async id => {
      const res = await fetch(`${base}/incidents/${encodeURIComponent(id)}/scenario`)
      return res.ok ? ((await res.json()) as Scenario) : null
    }))
    const scenarios = loaded.filter((item): item is Scenario => item !== null)
    useWorkspace.getState().addScenarios(scenarios, wanted === true ? scenarios[0]?.id : wanted ?? undefined)
    const target = wanted === true ? scenarios[0]?.id : wanted
    if (target) followIncident(target, base)
    return scenarios
  } catch {
    return []
  }
}

let source: EventSource | null = null

/** Follow one incident as it happens: the API re-sends the whole Scenario each time the audit log
 *  grows (Server-Sent Events) until the report is written. Idempotent per incident. */
export function followIncident(id: string, base = '/api'): void {
  const state = useWorkspace.getState()
  if (state.streaming === id || typeof EventSource === 'undefined') return
  const current = state.scenarios.find(item => item.id === id)
  if (current && current.live && current.complete) return
  source?.close()
  source = new EventSource(`${base}/incidents/${encodeURIComponent(id)}/stream`)
  useWorkspace.setState({ streaming: id })
  source.addEventListener('scenario', event => {
    const scenario = JSON.parse((event as MessageEvent).data) as Scenario
    const store = useWorkspace.getState()
    const isNew = !store.scenarios.some(item => item.id === scenario.id)
    store.updateScenario(scenario)
    if (isNew) {  // first frame of an incident that did not exist when the page loaded: show it, at "now"
      useWorkspace.getState().setScenario(scenario.id)
      useWorkspace.getState().seek(scenario.duration)
    }
  })
  const stop = () => { source?.close(); source = null; useWorkspace.setState({ streaming: null }) }
  source.addEventListener('done', stop)
  source.onerror = () => { if (source?.readyState === EventSource.CLOSED) stop() }
}
