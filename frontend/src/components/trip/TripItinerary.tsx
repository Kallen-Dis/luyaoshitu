import { freshLabel } from '../../lib/fresh'
import type { TripItem, TripPlanResult, TripSelection } from '../../types'
import { PLACE_MARK } from '../map/context'
import { tripMeters, tripMinutes } from '../../lib/trip'
import { TripSteps } from './TripSteps'
import { FreshFeedback } from './FreshFeedback'

export function TripItinerary({ result, selected, busy, onSelect, onReplace, onGuide }: {
  result: TripPlanResult; selected: string | null; busy: boolean;
  onSelect: (id: string) => void; onReplace: (index: number, selection: TripSelection) => void
  onGuide: (item: TripItem) => void
}) {
  return <div className="trip-itinerary"><p className="trip-start"><b>起</b> 当前起点</p>
    {result.legs.map((leg, index) => <section className="trip-leg" key={leg.entry_id}>
      <p className="trip-leg-distance">{result.basis === 'network' ? '步行' : '估算约'} {tripMeters(leg.walk_m)} · 约 {tripMinutes(leg.duration_s)}</p>
      <button className="trip-stop-name" type="button" onClick={() => onSelect(leg.entry_id)} aria-expanded={selected === leg.entry_id}>
        <b style={{ background: PLACE_MARK[leg.category]?.color }}>{PLACE_MARK[leg.category]?.glyph}</b>
        {leg.name}{leg.gate ? ` · ${leg.gate}` : ''}
      </button>
      {leg.fresh_status && <p className="trip-note" title={leg.fresh_evidence}>{freshLabel(leg.fresh_status)} · {leg.fresh_evidence}</p>}
      {leg.closure_status === 'blocked' && <p className="trip-blocked">围挡受阻，不能确认可通行</p>}
      <label className="trip-alternative">换一家 <select value="" disabled={busy} onChange={event => {
        const alternative = result.alternatives[index]?.items.find(a => a.entry_id === event.target.value)
        if (alternative) onReplace(index, alternative)
      }} aria-label={`第 ${index + 1} 站换一家（固定其他站点）`}>
        <option value="">固定其他站点</option>
        {(result.alternatives[index]?.items ?? []).map(a => <option value={a.entry_id} key={a.entry_id}>{a.name} · 合计 {tripMeters(a.total_m)}</option>)}
      </select></label>
      {selected === leg.entry_id && <>{leg.category === '生鲜采买' && <FreshFeedback place={leg} disabled={result.preview} />}<TripSteps item={leg} onGuide={onGuide} preview={result.preview} /></>}
    </section>)}
    <p className="trip-total">合计 {tripMeters(result.total_m)} · 约 {tripMinutes(result.total_s)}</p>
  </div>
}
