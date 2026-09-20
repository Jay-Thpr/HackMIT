import { useEffect, useMemo, useRef } from 'react'
import uPlot from 'uplot'
import 'uplot/dist/uPlot.min.css'
import { metricLabel, replay, type Scenario } from '../model'

export function MetricChart({ scenario, cursor, environmentId, nodeId, compact = false }: { scenario: Scenario; cursor: number; environmentId: string; nodeId: string; compact?: boolean }) {
  const container = useRef<HTMLDivElement>(null)
  const plot = useRef<uPlot>(null)
  const cursorRef = useRef(cursor)
  cursorRef.current = cursor
  const data = useMemo<uPlot.AlignedData>(() => {
    const times: number[] = []
    const latency: (number | null)[] = []
    const load: (number | null)[] = []
    const sampleTimes = new Set<number>(scenario.events.filter(event => event.at <= Math.floor(cursor)).map(event => event.at))
    for (let time = 0; time <= Math.floor(cursor); time += 2) sampleTimes.add(time)
    sampleTimes.add(Math.floor(cursor))
    for (const time of [...sampleTimes].sort((a, b) => a - b)) {
      const reading = replay(scenario, time).environments.find(env => env.id === environmentId)?.nodes[nodeId]
      times.push(time)
      latency.push(reading?.latency ?? null)
      load.push(reading?.qps ?? null)
    }
    return [times, latency, load]
  }, [scenario, Math.floor(cursor), environmentId, nodeId])
  useEffect(() => {
    if (!container.current) return
    const target = container.current
    const chart = new uPlot({
      width: Math.max(target.clientWidth, 200), height: compact ? 132 : 260,
      padding: [14, 16, 0, 0],
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { show: true } },
      select: { show: false, left: 0, top: 0, width: 0, height: 0 },
      scales: { x: { time: false, range: [0, scenario.duration] }, latency: { auto: true, range: (_plot, _min, max) => [0, Math.max(Number.isFinite(max) ? max * 1.15 : 1, 1)] }, load: { auto: true, range: (_plot, _min, max) => [0, Math.max(Number.isFinite(max) ? max * 1.15 : 1, 1)] } },
      axes: [
        { stroke: '#b9c2b7', font: '10px system-ui', grid: { show: false }, values: (_, ticks) => ticks.map(t => `${t}s`), size: 26, ticks: { show: false } },
        { scale: 'latency', stroke: '#b9c2b7', font: '10px system-ui', grid: { stroke: '#414a43', width: 1 }, values: (_, ticks) => ticks.map(t => `${t}`), size: 40, ticks: { show: false } },
        { scale: 'load', side: 1, stroke: '#b9c2b7', font: '10px system-ui', grid: { show: false }, size: 35, ticks: { show: false } },
      ],
      series: [ {}, { label: 'Latency · ms', scale: 'latency', stroke: '#ed9276', fill: 'rgba(182,76,60,0.045)', width: 1.7, paths: uPlot.paths.stepped!({ align: 1 }), points: { show: false }, spanGaps: false }, { label: 'Issued load · qps', scale: 'load', stroke: '#c3bb9a', dash: [4, 4], width: 1.5, paths: uPlot.paths.stepped!({ align: 1 }), points: { show: false }, spanGaps: false } ],
      hooks: { drawClear: [plot => {
        const ctx = plot.ctx
        const events = scenario.events.filter(event => event.at <= cursorRef.current && event.environmentId === environmentId)
        for (const action of events.filter(event => event.kind === 'action' && event.action)) {
          const release = events.find(event => event.kind === 'undo' && event.undoId === action.action!.id)
          const start = plot.valToPos(action.at, 'x', true)
          const end = plot.valToPos(release?.at ?? cursorRef.current, 'x', true)
          ctx.save()
          ctx.fillStyle = 'rgba(154,132,103,0.09)'
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
  const current = replay(scenario, cursor).environments.find(env => env.id === environmentId)?.nodes[nodeId]
  return <div className="metric-chart" role="img" aria-label={`Illustrative latency and issued load for ${nodeId} in ${environmentId}, through ${Math.floor(cursor)} seconds. Current latency: ${metricLabel(current?.latency, 'ms')}; issued load: ${metricLabel(current?.qps, 'qps')}. Blank regions mean no samples, not zero.`} ref={container} />
}
