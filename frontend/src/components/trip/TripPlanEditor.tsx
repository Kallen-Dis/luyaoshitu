import { useRef, useState } from 'react'
import type { TripOrigin, TripStopOption } from '../../types'
import { originLabel, tripSegmentColor } from '../../lib/itinerary'
import { PLACE_MARK, SHORT_NAME } from '../map/context'

export function TripPlanEditor({ origin, stops, destinations, failed, busy, picking, onStops, onPickDestination, onPickOrigin, onResetOrigin }: {
  origin: TripOrigin; stops: string[]; failed: string[]; busy: boolean; picking: boolean
  destinations: Record<string, TripStopOption>; onPickDestination: (index: number) => void
  onStops: (stops: string[]) => void; onPickOrigin: () => void; onResetOrigin: () => void
}) {
  const [categoryTarget, setCategoryTarget] = useState<string | null>(null)
  const root = useRef<HTMLDivElement>(null)
  const categories = Object.keys(PLACE_MARK)
  const maxStops = categories.length
  const closeCategory = (target: string) => {
    setCategoryTarget(null)
    requestAnimationFrame(() => root.current?.querySelector<HTMLButtonElement>(`[data-category-trigger="${target}"]`)?.focus())
  }
  const move = (index: number, direction: number) => {
    const next = [...stops]; [next[index], next[index + direction]] = [next[index + direction], next[index]]; onStops(next)
  }
  return <div className="trip-plan-editor" ref={root}>
    <p className="trip-editor-stage">设置行程<span>按你的顺序步行</span></p>
    <h3>出发位置</h3>
    <div className="trip-editor-origin"><b className="trip-node origin">起</b><strong>{originLabel(origin)}</strong>
      <button type="button" className="trip-text-button" onClick={onPickOrigin} disabled={busy || picking}>{picking ? '正在选点' : '在地图上更换'}</button>
    </div>
    <details className="trip-origin-detail"><summary>位置详情</summary><p>{origin.lat.toFixed(6)}, {origin.lng.toFixed(6)}</p>
      {origin.kind !== 'center' && <button type="button" disabled={busy} onClick={onResetOrigin}>回到分析中心</button>}
    </details>
    {picking && <p className="trip-inline-notice" role="status">在地图上点击一个位置，作为新起点。</p>}
    <h3>到访顺序</h3><p className="trip-note">直接选择想去的设施，也可以保留自动推荐。</p>
    <ol className="trip-edit-stops">
      {stops.map((stop, index) => <li className="trip-draft-stop" key={stop} style={{ '--leg-color': tripSegmentColor(index) } as React.CSSProperties}>
        <div className="trip-draft-head"><b className="trip-node">{index + 1}</b><strong>第 {index + 1} 站</strong>
        <button type="button" data-category-trigger={stop} className="trip-category-trigger" aria-expanded={categoryTarget === stop} aria-controls={`trip-category-${index}`} aria-label={`更换第 ${index + 1} 站类别，当前${SHORT_NAME[stop]}`} disabled={busy} onClick={() => setCategoryTarget(categoryTarget === stop ? null : stop)}><span style={{ color: PLACE_MARK[stop]?.color }}>{PLACE_MARK[stop]?.glyph}</span>{SHORT_NAME[stop]}<span aria-hidden="true">⌄</span></button></div>
        {categoryTarget === stop && <div id={`trip-category-${index}`}><CategoryChoices value={stop} used={stops} failed={failed} disabled={busy} onChoose={category => { onStops(stops.map((c, i) => i === index ? category : c)); closeCategory(category) }} onClose={() => closeCategory(stop)} /></div>}
        <button type="button" data-destination-index={index} className="trip-destination-choice" disabled={busy || picking} onClick={() => onPickDestination(index)} aria-label={`第 ${index + 1} 站${destinations[stop] ? '更换' : '选择'}具体设施`}>
          <span><strong>{destinations[stop]?.name ?? '自动推荐一处'}</strong><small>{destinations[stop]?.gate ? `到达入口：${destinations[stop].gate}` : destinations[stop] ? '已指定目的地' : '尚未指定具体设施'}</small></span><span className="trip-destination-link">{destinations[stop] ? '更换' : '选择设施'} ›</span>
        </button>
        <div className="trip-draft-actions"><span className={`trip-choice-badge${destinations[stop] ? '' : ' auto'}`}>{destinations[stop] ? '已指定' : '自动推荐'}</span>
        <div className="trip-edit-order"><button type="button" disabled={busy || index === 0} aria-label={`第 ${index + 1} 站上移`} onClick={() => move(index, -1)}>↑</button>
          <button type="button" disabled={busy || index === stops.length - 1} aria-label={`第 ${index + 1} 站下移`} onClick={() => move(index, 1)}>↓</button></div>
        <button type="button" className="trip-remove-stop" disabled={busy} aria-label={`移除第 ${index + 1} 站`} onClick={() => onStops(stops.filter((_, i) => i !== index))}>移除</button></div>
      </li>)}
    </ol>
    {stops.length < maxStops && <div className="trip-add-section"><button type="button" className="trip-add-station" data-category-trigger="add" aria-expanded={categoryTarget === 'add'} aria-controls="trip-add-categories" disabled={busy} onClick={() => setCategoryTarget(categoryTarget === 'add' ? null : 'add')}><span aria-hidden="true">＋</span><span>添加一站<small>选择设施类别</small></span><span aria-hidden="true">{categoryTarget === 'add' ? '−' : '›'}</span></button>
      {categoryTarget === 'add' && <div id="trip-add-categories"><CategoryChoices used={stops} failed={failed} disabled={busy} onChoose={category => { onStops([...stops, category]); closeCategory(category) }} onClose={() => closeCategory('add')} /></div>}
    </div>}
    <p className="trip-note">已安排 {stops.length} / {maxStops} 站 · 每类一站，按设置的顺序走。</p>
  </div>
}

function CategoryChoices({ value, used, failed, disabled, onChoose, onClose }: {
  value?: string; used: string[]; failed: string[]; disabled: boolean
  onChoose: (category: string) => void; onClose: () => void
}) {
  return <div className="trip-category-choices" role="group" aria-label="六类设施，可选类别" onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose() } }}>
    {Object.keys(PLACE_MARK).map(category => {
      const current = category === value
      const reason = failed.includes(category) ? '检索失败' : used.includes(category) && !current ? '已添加' : null
      return <button type="button" key={category} disabled={disabled || Boolean(reason)} aria-pressed={current} onClick={() => { if (current) onClose(); else onChoose(category) }} style={{ '--category-color': PLACE_MARK[category].color } as React.CSSProperties}>
        <b>{PLACE_MARK[category].glyph}</b><span>{SHORT_NAME[category]}<small>{current ? '当前类别' : reason ?? '可选择'}</small></span>
      </button>
    })}
  </div>
}
