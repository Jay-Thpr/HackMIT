import { create } from 'zustand'
import { replay, type Scenario } from './model'
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
  startDemo: (id?: string) => void
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
  cursor: 0,
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
  setScenario: id => set({ scenarioId: id, cursor: 0, environmentId: 'production', isolatedLayer: null, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined, selectedEvent: undefined, playing: false, follow: false, focusRevision: get().focusRevision + 1 }),
  startDemo: id => {
    get().setScenario(id ?? get().scenarioId)
    set({ playing: true, follow: true, speed: 1, view: 'investigation' })
  },
  seek: time => {
    const state = get()
    const scenario = state.scenarios.find(item => item.id === state.scenarioId)!
    const cursor = Math.max(0, Math.min(time, scenario.duration))
    // Rewinding drops a selection only when that environment no longer exists at the new time.
    const reset = !replay(scenario, cursor).environments.some(environment => environment.id === state.environmentId)
    set({ cursor, playing: false, follow: false, selectedEvent: undefined, ...(reset ? { environmentId: 'production', isolatedLayer: null, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined, focusRevision: state.focusRevision + 1 } : {}) })
  },
  tick: delta => {
    const state = get()
    if (!state.playing) return
    const scenario = state.scenarios.find(item => item.id === state.scenarioId)!
    const duration = scenario.duration
    const cursor = Math.min(duration, state.cursor + delta * state.speed)
    set({ cursor, playing: cursor < duration || Boolean(scenario.live && !scenario.complete) })
  },
  togglePlay: () => {
    const state = get()
    const duration = state.scenarios.find(item => item.id === state.scenarioId)!.duration
    if (!state.playing && (state.cursor === 0 || state.cursor >= duration)) { get().startDemo(); return }
    set({ playing: !state.playing })
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
    const atEnd = previous ? state.cursor >= previous.duration - 1 : false
    const cursor = state.scenarioId !== item.id ? state.cursor : state.playing && atEnd ? item.duration : Math.min(state.cursor, item.duration)
    set({ scenarios, cursor })
  },
  inspect: (selectedNode, environmentId) => set({ selectedSuiteCheck: undefined, selectedAgent: undefined, selectedNode, environmentId, isolatedLayer: environmentId, traceTab: 'trace', follow: false, selectedEvent: undefined, focusRevision: get().focusRevision + 1 }),
  focus: environmentId => set({ environmentId, isolatedLayer: environmentId, follow: false, selectedNode: undefined, selectedSuiteCheck: undefined, selectedAgent: undefined, focusRevision: get().focusRevision + 1 }),
  set: patch => set(patch),
}))
