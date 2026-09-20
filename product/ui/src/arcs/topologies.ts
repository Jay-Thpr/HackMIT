import type { Scenario } from '../model'

type ExistingScenario = Omit<Scenario, 'events'>

// Arc scenarios retain the exact topology and complete baseline from the existing
// commerce/platform scenarios. No hidden state or production data enters a clone.
export function existingTopology(source: ExistingScenario) {
  return { topology: source.topology, baseline: source.baseline }
}

