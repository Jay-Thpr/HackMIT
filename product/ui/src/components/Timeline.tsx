import { Pause, Play, RotateCcw, SkipForward } from 'lucide-react'
import { timeLabel, type Scenario } from '../model'
import { useWorkspace } from '../store'

export function Timeline({ scenario }: { scenario: Scenario }) {
  const { cursor, playing, speed, seek, togglePlay, set } = useWorkspace()
  const next = scenario.events.find(event => event.at > cursor)
  return <div className="timeline">
    <div className="playback-controls">
      <button className="play-button" aria-label={playing ? 'Pause simulation' : 'Play simulation'} onClick={togglePlay}>{playing ? <Pause size={15} /> : <Play size={15} fill="currentColor" />}</button>
      <button className="icon-button" aria-label="Restart simulation" onClick={() => seek(0)}><RotateCcw size={15} /></button>
      <button className="icon-button" aria-label="Next event" disabled={!next} onClick={() => next && seek(next.at)}><SkipForward size={16} /></button>
      <span className="timeline-time">{timeLabel(cursor)} <span>/ {timeLabel(scenario.duration)}</span></span>
    </div>
    <div className="scrubber">
      <input aria-label="Simulation timeline" type="range" min={0} max={scenario.duration} step={1} value={cursor} onChange={event => seek(Number(event.target.value))} style={{ '--progress': `${cursor / scenario.duration * 100}%` } as React.CSSProperties} />
      <div className="timeline-markers"><span>Observe</span><span>Reproduce</span><span>Compare</span><span>Confirm</span></div>
    </div>
    <button className="speed-button" aria-label={`Playback speed ${speed} times`} onClick={() => set({ speed: speed === 1 ? 2 : speed === 2 ? 4 : 1 })}>{speed}×</button>
    <span className="timeline-mode">SIMULATED REPLAY</span>
  </div>
}
