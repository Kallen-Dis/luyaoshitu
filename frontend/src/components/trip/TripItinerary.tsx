import { freshLabel } from '../../lib/fresh'
import { itineraryLegIssue, tripSegmentColor } from '../../lib/itinerary'
import type { TripItem, TripPlanResult } from '../../types'
import { PLACE_MARK, SHORT_NAME } from '../map/context'
import { tripMeters, tripMinutes } from '../../lib/trip'
import { TripSteps } from './TripSteps'
import { FreshFeedback } from './FreshFeedback'

export function TripItinerary({ result, selected, busy, originName, onSelect, onChangeStop, onGuide }: {
  result: TripPlanResult; selected: string | null; busy: boolean; originName: string
  onSelect: (id: string | null) => void; onChangeStop: (index: number) => void
  onGuide: (item: TripItem) => void
}) {
  const index = result.legs.findIndex(leg => leg.entry_id === selected)
  const active = result.legs[index]
  if (active) return <div className="trip-segment-detail" key={active.entry_id} style={{ '--leg-color': tripSegmentColor(index) } as React.CSSProperties}>
    <div className="trip-segment-endpoints"><div><b className="trip-node" style={{ '--leg-color': index ? tripSegmentColor(index - 1) : '#1c3a28' } as React.CSSProperties}>{index || '起'}</b><span>{index ? result.legs[index - 1].name : originName}</span></div>
      <span className="trip-endpoint-arrow" aria-hidden="true">↓</span>
      <div><b className={`trip-node${itineraryLegIssue(active, result.preview) ? ' uncertain' : ''}`}>{index + 1}</b><span><strong>{active.name}</strong><small>{active.gate ? `到达入口：${active.gate}` : active.distance_basis === 'navigation_point' ? '按导航点测距' : '按设施坐标测距'}</small></span></div>
    </div>
    <LegStatus leg={active} preview={result.preview} />
    <button type="button" className="trip-change-destination" data-destination-index={index} disabled={busy} onClick={() => onChangeStop(index)}>更换第 {index + 1} 站的设施<span aria-hidden="true">›</span></button>
    <p className="trip-note">其他站点与入口保持不变，选定后更新相邻路段。</p>
    {active.fresh_status && <div className="trip-segment-evidence"><span>{freshLabel(active.fresh_status)}</span><p>{active.fresh_evidence}</p></div>}
    {active.category === '生鲜采买' && <FreshFeedback place={active} disabled={result.preview} />}
    <TripSteps item={active} onGuide={onGuide} preview={result.preview} showLaunch={false} />
  </div>
  return <ol className="trip-timeline" aria-label="按顺序到访的行程站点">
    <li className="trip-timeline-origin"><b className="trip-node origin">起</b><strong>{originName}</strong></li>
    {result.legs.map((leg, i) => {
      const issue = itineraryLegIssue(leg, result.preview)
      return <li key={leg.entry_id} className="trip-timeline-stop" style={{ '--leg-color': tripSegmentColor(i) } as React.CSSProperties}>
        <p className="trip-timeline-distance"><span>第 {i + 1} 段</span>{tripMeters(leg.walk_m)} · 约 {tripMinutes(leg.duration_s)}{result.basis !== 'network' ? ' · 估算' : ''}</p>
        <button type="button" className="trip-timeline-select" onClick={() => onSelect(leg.entry_id)} aria-label={`第 ${i + 1} 站：${leg.name}，查看对应路段`}>
          <b className={`trip-node${issue ? ' uncertain' : ''}${leg.closure_status === 'blocked' ? ' blocked' : ''}`}>{i + 1}</b>
          <span className="trip-timeline-copy"><strong>{leg.name}</strong><small>{SHORT_NAME[leg.category]}{leg.gate ? ` · ${leg.gate}` : leg.category === '基础教育' ? leg.distance_basis === 'navigation_point' ? ' · 导航点' : ' · 按坐标测距' : ''}{i === result.legs.length - 1 ? ' · 终点' : ''}</small></span>
          <span className="trip-timeline-glyph" style={{ color: PLACE_MARK[leg.category]?.color }} aria-hidden="true">{PLACE_MARK[leg.category]?.glyph}</span><span className="trip-timeline-chevron" aria-hidden="true">›</span>
        </button>
        <div className="trip-stop-actions"><button type="button" className="trip-segment-link" onClick={() => onSelect(leg.entry_id)}>查看路段 ›</button><button type="button" className="trip-change-destination" data-destination-index={i} disabled={busy} onClick={() => onChangeStop(i)}>更换这一站</button></div>
        {issue && <p className={`trip-timeline-issue${leg.closure_status === 'blocked' ? ' blocked' : ''}`}>{issue}</p>}
      </li>
    })}
  </ol>
}

function LegStatus({ leg, preview }: { leg: TripItem; preview?: boolean }) {
  const issue = itineraryLegIssue(leg, preview)
  return <p className={`trip-route-status${issue ? ' uncertain' : ''}${leg.closure_status === 'blocked' ? ' blocked' : ''}`}><span aria-hidden="true">{issue ? '!' : '✓'}</span>{issue ?? '本段路线已取得'}</p>
}
