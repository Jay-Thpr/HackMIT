import { create } from 'zustand'
import type { Scenario } from './model'
import { scenarios as synthetic } from './scenarios'

export type View = 'explanation' | 'investigation' | 'observability' | 'experiments' | 'replay'

interface UIState {
  scenarios: Scenario[]
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
  addScenarios: (items: Scenario[], select?: string) => void
  updateScenario: (item: Scenario) => void
  streaming: string | null
  seek: (time: number) => void
  tick: (delta: number) => void
  togglePlay: () => void
  inspect: (node: string, environmentId: string) => void
  focus: (environmentId: string) => void
  set: (patch: Partial<Omit<UIState, 'set'>>) => void
}

const prefersReducedMotion = typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

export const useWorkspace = create<UIState>((set, get) => ({
  scenarios: synthetic,
  scenarioId: synthetic[0].id,
  streaming: null,
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
    const scenario = get().scenarios.find(item => item.id === get().scenarioId)!
    set({ cursor: Math.max(0, Math.min(time, scenario.duration)), playing: false, selectedEvent: undefined })
  },
  tick: delta => {
    const state = get()
    if (!state.playing) return
    const duration = state.scenarios.find(item => item.id === state.scenarioId)!.duration
    const cursor = Math.min(duration, state.cursor + delta * state.speed)
    set({ cursor, playing: cursor < duration })
  },
  togglePlay: () => {
    const state = get()
    const duration = state.scenarios.find(item => item.id === state.scenarioId)!.duration
    set({ playing: !state.playing, cursor: state.cursor >= duration ? 0 : state.cursor })
  },
  addScenarios: (items, select) => {
    const known = new Set(get().scenarios.map(item => item.id))
    const fresh = items.filter(item => !known.has(item.id))
    if (fresh.length) set({ scenarios: [...fresh, ...get().scenarios] })  // live incidents first, newest first
    if (select && get().scenarios.some(item => item.id === select)) get().setScenario(select)
  },
  updateScenario: item => {
    // A live incident grew: swap the scenario in place. The cursor is kept, except that a viewer
    // sitting at the end of the replay (watching it happen) is carried forward to the new end.
    const state = get()
    const previous = state.scenarios.find(s => s.id === item.id)
    const scenarios = previous ? state.scenarios.map(s => (s.id === item.id ? item : s)) : [item, ...state.scenarios]
    const atEnd = previous ? state.cursor >= previous.duration - 1 : true
    const cursor = state.scenarioId === item.id && atEnd ? item.duration : Math.min(state.cursor, item.duration)
    set({ scenarios, cursor })
  },
  inspect: (selectedNode, environmentId) => set({ selectedSuiteCheck: undefined, selectedAgent: undefined, selectedNode, environmentId, isolatedLayer: environmentId, traceTab: 'trace', follow: false, selectedEvent: undefined, focusRevision: get().focusRevision + 1 }),
  focus: environmentId => set({ environmentId, isolatedLayer: environmentId, follow: false, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined, focusRevision: get().focusRevision + 1 }),
  set: patch => set(patch),
}))
