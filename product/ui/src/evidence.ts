import { replay, type Scenario } from './model'

export interface EvidenceBundle {
  schema_version: 'faultline-evidence/1'
  incident: string
  scenario: string
  title: string
  recorded: { startedAt: string; endedAt: string } | null
  report: Scenario['report'] | null
  topology: { nodes: { id: string; kind: string; instrumented: boolean; tenants?: string[] }[]; edges: { source: string; target: string }[] }
  baseline: Scenario['baseline']
  events: Scenario['events']
  outcome: {
    diagnosis?: string
    confirmed?: boolean
    productionActions: number
    rollbackFailures: number
    observer?: { environmentId: string; diagnosis?: string; abstained: boolean }
  }
}

/** The evidence a recorded run can hand over, serialised from what the workspace is displaying.
 *  Deliberately derived rather than re-stated: the export cannot drift from the replay. */
export function evidenceBundle(scenario: Scenario): EvidenceBundle {
  const end = replay(scenario, scenario.duration)
  return {
    schema_version: 'faultline-evidence/1',
    incident: scenario.incident,
    scenario: scenario.id,
    title: scenario.incidentTitle,
    recorded: scenario.report ? { startedAt: scenario.report.startedAt, endedAt: scenario.report.endedAt } : null,
    report: scenario.report ?? null,
    topology: {
      nodes: scenario.topology.nodes.map(node => ({
        id: node.id, kind: node.kind, instrumented: node.instrumented,
        ...(node.tenants ? { tenants: node.tenants } : {}),
      })),
      edges: scenario.topology.edges.map(edge => ({ source: edge.source, target: edge.target })),
    },
    baseline: scenario.baseline,
    events: scenario.events,
    outcome: {
      diagnosis: end.diagnosis,
      confirmed: end.confirmed,
      productionActions: end.actions.filter(action => action.environmentId === 'production').length,
      rollbackFailures: end.actions.filter(action => action.status === 'release-failed').length,
      observer: end.observer,
    },
  }
}

export function evidenceFilename(scenario: Scenario): string {
  return `${scenario.incident.replace(/[^A-Za-z0-9-]+/g, '-')}-evidence.json`
}

/** Triggers the download. Separated from `evidenceBundle` so the bundle stays testable
 *  without a DOM. */
export function exportEvidence(scenario: Scenario): void {
  const blob = new Blob([JSON.stringify(evidenceBundle(scenario), null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = evidenceFilename(scenario)
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
