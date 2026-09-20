import type { NodeReading, Scenario, WorkspaceEvent } from '../model'

export type ArcScenario = Omit<Scenario, 'events'>

export interface ObserverConclusion {
  at: number
  title: string
  detail: string
  diagnosis: string
  abstained?: boolean
  targetId?: string
  evidence: string[]
  hypotheses: string[]
  recommendation: string
}

export interface CloneTrack {
  id: 'clone-a' | 'clone-b'
  actor: 'investigator-a' | 'investigator-b'
  label: string
  color: string
  hypothesisId: string
  hypothesisTitle: string
  reproduceTitle: string
  reproduceDetail: string
  reproduceTarget: string
  reproduceArgs: Record<string, string | number>
  reproduceReadings: Record<string, NodeReading>
  probeTitle: string
  probeDetail: string
  probeTarget: string
  probeArgs: Record<string, string | number>
  probeReadings: Record<string, NodeReading>
  probeResult: string
}

export interface FinalProbe {
  environmentId: 'production' | 'clone-a' | 'clone-b'
  actor: 'adapter' | 'investigator-a' | 'investigator-b'
  title: string
  detail: string
  targetId: string
  args: Record<string, string | number>
  action: { id: string; label: string; ttl: number }
  readings: Record<string, NodeReading>
  releaseTitle: string
  releaseDetail: string
  releaseReadings?: Record<string, NodeReading>
}

export interface ConfirmSeparatingConfig {
  scenario: ArcScenario
  incidentReadings: Record<string, NodeReading>
  detectionDetail: string
  hypothesisDetail: string
  observer: ObserverConclusion
  clones: [CloneTrack, CloneTrack]
  disagreementTitle: string
  disagreementDetail: string
  separation?: { z: number; sigma: number }
  finalProbe: FinalProbe
  verdictTitle: string
  verdictDetail: string
  verdictDiagnosis: string
  verdictTarget: string
}

export type EventFactory = (at: number, kind: WorkspaceEvent['kind'], title: string, extra?: Partial<WorkspaceEvent>) => WorkspaceEvent
