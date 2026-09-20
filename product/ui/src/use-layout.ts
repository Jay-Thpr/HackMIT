import { useEffect, useState } from 'react'
import ELK from 'elkjs/lib/elk-api'
import ElkWorker from 'elkjs/lib/elk-worker.min.js?worker'
import { layoutGraph, type GraphLayout } from './layout'
import type { Topology } from './model'

const cache = new Map<string, GraphLayout>()

export function useLayout(topology: Topology) {
  const key = JSON.stringify([topology.nodes.map(node => node.id), topology.edges])
  const [state, setState] = useState<{ key: string; layout?: GraphLayout; error?: string }>({ key })
  useEffect(() => {
    let active = true
    if (cache.has(key)) {
      setState({ key, layout: cache.get(key) })
      return
    }
    const worker = new ElkWorker()
    const engine = new ELK({ workerFactory: () => worker })
    layoutGraph(topology, engine).then(layout => {
      cache.set(key, layout)
      if (active) setState({ key, layout })
    }).catch(() => {
      if (active) setState({ key, error: 'The spatial layout could not load. The service list is still available.' })
    }).finally(() => worker.terminate())
    return () => { active = false; worker.terminate() }
  }, [key, topology])
  return state.key === key ? state : { key }
}
