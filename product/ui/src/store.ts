import { create } from 'zustand'
import { scenarios } from './scenarios'

export type View = 'explanation' | 'investigation' | 'observability' | 'experiments' | 'replay'

interface UIState {
  scenarioId: string
  cursor: number
  playing: boolean
  speed: number
  environmentId: string
  isolatedLayer: string | null
  selectedAgent?: string
  selectedSuiteCheck?: { environmentId: string; checkId: string }
  selectedNode?: string
  selectedEvent?: string
  view: View
  traceTab: 'evidence' | 'trace'
  follow: boolean
  reducedMotion: boolean
  focusRevision: number
  dialog: 'experiment' | 'safety' | null
  setScenario: (id: string) => void
  seek: (time: number) => void
  tick: (delta: number) => void
  togglePlay: () => void
  inspect: (node: string, environmentId: string) => void
  focus: (environmentId: string) => void
  set: (patch: Partial<Omit<UIState, 'set'>>) => void
}

const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export const useWorkspace = create<UIState>((set, get) => ({
  scenarioId: scenarios[0].id,
  cursor: 47,
  playing: false,
  speed: 1,
  environmentId: 'production',
  isolatedLayer: null,
  view: 'investigation',
  traceTab: 'evidence',
  follow: false,
  reducedMotion: prefersReducedMotion,
  focusRevision: 0,
  dialog: null,
  setScenario: id => set({ scenarioId: id, cursor: 47, environmentId: 'production', isolatedLayer: null, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined, selectedEvent: undefined, playing: false, focusRevision: get().focusRevision + 1 }),
  seek: time => {
    const scenario = scenarios.find(item => item.id === get().scenarioId)!
    set({ cursor: Math.max(0, Math.min(time, scenario.duration)), playing: false, selectedEvent: undefined })
  },
  tick: delta => {
    const state = get()
    if (!state.playing) return
    const duration = scenarios.find(item => item.id === state.scenarioId)!.duration
    const cursor = Math.min(duration, state.cursor + delta * state.speed)
    set({ cursor, playing: cursor < duration })
  },
  togglePlay: () => {
    const state = get()
    const duration = scenarios.find(item => item.id === state.scenarioId)!.duration
    set({ playing: !state.playing, cursor: state.cursor >= duration ? 0 : state.cursor })
  },
  inspect: (selectedNode, environmentId) => set({ selectedSuiteCheck: undefined, selectedAgent: undefined, selectedNode, environmentId, isolatedLayer: environmentId, traceTab: 'trace', follow: false, selectedEvent: undefined, focusRevision: get().focusRevision + 1 }),
  focus: environmentId => set({ environmentId, isolatedLayer: environmentId, follow: false, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined, focusRevision: get().focusRevision + 1 }),
  set: patch => set(patch),
}))
