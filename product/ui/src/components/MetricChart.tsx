import { useEffect, useMemo, useRef } from 'react'
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'
import { replay, type Scenario } from '../model'

export function MetricChart({ scenario, cursor, environmentId, nodeId, compact = false }: { scenario: Scenario; cursor: number; environmentId: string; nodeId: string; compact?: boolean }) {
  const container = useRef<HTMLDivElement>(null)
  const plot = useRef<uPlot>(null)
  const cursorRef = useRef(cursor)
  cursorRef.current = cursor
  const data = useMemo<uPlot.AlignedData>(() => {
    const times: number[] = []
    const latency: (number | null)[] = []
    const load: (number | null)[] = []
    for (let time = 0; time <= Math.floor(cursor); time += 2) {
      const reading = replay(scenario, time).environments.find(env => env.id === environmentId)?.nodes[nodeId]
      times.push(time)
      latency.push(reading?.latency ?? null)
      load.push(reading?.qps ?? null)
    }
    return [times, latency, load]
  }, [scenario, Math.floor(cursor / 2), environmentId, nodeId])
  useEffect(() => {
    if (!container.current) return
    const target = container.current
    const chart = new uPlot({
      width: Math.max(target.clientWidth, 200), height: compact ? 132 : 260,
      padding: [14, 16, 0, 0],
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { show: true } },
      select: { show: false, left: 0, top: 0, width: 0, height: 0 },
      scales: { x: { time: false, range: [0, scenario.duration] }, latency: { range: [0, 2200] }, load: { range: [0, 400] } },
      axes: [
        { stroke: '#8b948c', font: '10px system-ui', grid: { show: false }, values: (_, ticks) => ticks.map(t => `${t}s`), size: 26, ticks: { show: false } },
        { scale: 'latency', stroke: '#8b948c', font: '10px system-ui', grid: { stroke: '#e9ece6', width: 1 }, values: (_, ticks) => ticks.map(t => `${t}`), size: 40, ticks: { show: false } },
        ...(compact ? [] : [{ scale: 'load', side: 1, stroke: '#8b948c', font: '10px system-ui', grid: { show: false }, size: 35, ticks: { show: false } } as uPlot.Axis]),
      ],
      series: [ {}, { label: 'Latency · ms', scale: 'latency', stroke: '#b37b54', fill: 'rgba(179,123,84,0.045)', width: 1.7, paths: uPlot.paths.stepped!({ align: 1 }), points: { show: false }, spanGaps: false }, { label: 'Issued load · qps', scale: 'load', stroke: '#5a8b78', dash: [4, 4], width: 1.5, paths: uPlot.paths.stepped!({ align: 1 }), points: { show: false }, spanGaps: false } ],
      hooks: { drawClear: [plot => {
        const ctx = plot.ctx
        const events = scenario.events.filter(event => event.at <= cursorRef.current && event.environmentId === environmentId)
        for (const action of events.filter(event => event.kind === 'action' && event.action)) {
          const release = events.find(event => event.kind === 'undo' && event.undoId === action.action!.id)
          const start = plot.valToPos(action.at, 'x', true)
          const end = plot.valToPos(release?.at ?? cursorRef.current, 'x', true)
          ctx.save()
          ctx.fillStyle = 'rgba(80,133,108,0.07)'
          ctx.fillRect(start, plot.bbox.top, end - start, plot.bbox.height)
          ctx.restore()
        }
      }] },
    }, data, target)
    plot.current = chart
    const observer = new ResizeObserver(entries => {
      const width = entries[0]?.contentRect.width
      if (width && width > 0) chart.setSize({ width, height: compact ? 132 : 260 })
    })
    observer.observe(target)
    return () => { observer.disconnect(); chart.destroy(); plot.current = null }
  }, [scenario.id, compact, environmentId, nodeId])
  useEffect(() => { plot.current?.setData(data) }, [data])
  return <div className="metric-chart" role="img" aria-label={`Illustrative latency and issued load for ${nodeId} in ${environmentId}, through ${Math.floor(cursor)} seconds. Exact current values are available in the metric cards.`} ref={container} />
}
