import { useEffect, useRef } from 'react'
import { freshLabel } from '../../lib/fresh'
import type { TripItem } from '../../types'
import { FreshFeedback } from './FreshFeedback'
import { tripMeters, tripMinutes } from '../../lib/trip'
import { TripSteps } from './TripSteps'

export function TripNearestList({ items, selected, estimate, onSelect, onGuide, preview }: {
  items: TripItem[]; selected: string | null; estimate: boolean;
  onGuide: (item: TripItem) => void; preview?: boolean
  onSelect: (id: string) => void
}) {
  const root = useRef<HTMLDivElement>(null)
  const previous = useRef<string | null>(null)
  useEffect(() => {
    // 首次响应的 selection 可能晚于列表挂载，初始 null→第 1 家也不自动滚动。
    if (previous.current && selected && previous.current !== selected) root.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })
    previous.current = selected
  }, [selected])
  return <div ref={root} className="trip-results" role="listbox" aria-label="已检索设施中的最近几家">
    {items.map((item, index) => <div className={item.entry_id === selected ? 'trip-result selected' : 'trip-result'} key={item.entry_id}>
      <button type="button" role="option" aria-selected={item.entry_id === selected}
        tabIndex={item.entry_id === selected || (!selected && index === 0) ? 0 : -1}
        onClick={() => onSelect(item.entry_id)} onKeyDown={event => {
          if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
          event.preventDefault()
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
          onSelect(items[next].entry_id)
          root.current?.querySelectorAll<HTMLButtonElement>('[role="option"]')[next]?.focus()
        }}>
        <span className="trip-rank">{index + 1}</span><span className="trip-result-copy"><b>{item.name}</b>
          {item.fresh_status && <small className={`trip-fresh-badge ${item.fresh_status}`} title={item.fresh_evidence}>{freshLabel(item.fresh_status)}</small>}
          <span className="trip-distance"><strong>{tripMeters(item.walk_m)}</strong><span>{estimate ? '估算' : '步行'} · 约 {tripMinutes(item.duration_s)}</span></span>
          <small>直线 {tripMeters(item.straight_m)}{item.detour != null ? ` · 绕行 ${item.detour.toFixed(2)} 倍` : ''}
            {item.route ? ` · 过街 ${item.route.crossings['过街']} 次` : ''}</small>
          {item.closure_status === 'blocked' && <small className="trip-blocked">围挡受阻，不能确认可通行</small>}
        </span>
      </button>
      {item.entry_id === selected && <>
        {item.category === '生鲜采买' && <div className="trip-fresh-detail">{item.fresh_evidence && <p>{item.fresh_evidence}</p>}<FreshFeedback place={item} disabled={preview} /></div>}
        <TripSteps item={item} onGuide={onGuide} preview={preview} />
      </>}
    </div>)}
  </div>
}
