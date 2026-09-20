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
    const loaded = await Promise.all(incidents.slice(0, 12).map(async ({ id }) => {
      const res = await fetch(`${base}/incidents/${encodeURIComponent(id)}/scenario`)
      return res.ok ? ((await res.json()) as Scenario) : null
    }))
    const scenarios = loaded.filter((item): item is Scenario => item !== null)
    const wanted = requestedIncident()
    useWorkspace.getState().addScenarios(scenarios, wanted === true ? scenarios[0]?.id : wanted ?? undefined)
    return scenarios
  } catch {
    return []
  }
}
