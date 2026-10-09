import { useEffect, useMemo, useRef, useState } from 'react'
import type { TripSelection, TripStopOption } from '../../types'
import { tripMeters } from '../../lib/trip'
import { freshLabel } from '../../lib/fresh'
import { tripSegmentColor } from '../../lib/itinerary'
import { SHORT_NAME } from '../map/context'
import { matchesTripQuery } from '../../lib/tripSearch'

export function TripStopPicker({ index, category, options, current, loading, error, allowAuto, canLookup, lookupBusy, lookupError, lookupWarnings, onLookup, onAddMissing, onChoose, onBack, onRetry }: {
  index: number; category: string; options: TripStopOption[]; current?: TripSelection
  loading: boolean; error?: string | null; allowAuto: boolean
  canLookup: boolean; lookupBusy: boolean; lookupError?: string; lookupWarnings: string[]
  onLookup: (query: string) => void; onAddMissing: () => void
  onChoose: (option: TripStopOption | null) => void; onBack: () => void; onRetry: () => void
}) {
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(30)
  const input = useRef<HTMLInputElement>(null)
  useEffect(() => { input.current?.focus() }, [])
  const groups = useMemo(() => {
    const grouped = new Map<string, TripStopOption[]>()
    for (const option of options) grouped.set(option.place_id, [...(grouped.get(option.place_id) ?? []), option])
    return [...grouped.values()].filter(group => matchesTripQuery(group[0].name, group.map(o => o.gate), query, group[0].aliases))
  }, [options, query])
  return <section className="trip-stop-picker" style={{ '--leg-color': tripSegmentColor(index) } as React.CSSProperties}>
    <button type="button" className="trip-back" onClick={onBack}>← {allowAuto ? '返回设置行程' : '返回原行程'}</button>
    <div className="trip-picker-heading"><b className="trip-node">{index + 1}</b><div><h3>选择第 {index + 1} 站</h3><p>{SHORT_NAME[category]} · 选择具体设施{category === '基础教育' ? '及到达入口' : ''}</p></div></div>
    <div className="trip-picker-search">
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" stroke="currentColor" strokeWidth="1.7" /><path d="m16 16 4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg>
      <input ref={input} value={query} onChange={event => { setQuery(event.target.value); setLimit(30) }} placeholder="筛选设施名称或入口" aria-label="筛选当前可选设施或入口" aria-describedby="trip-search-scope" maxLength={60} />
      {query && <button type="button" className="trip-search-clear" aria-label="清空筛选" onClick={() => { setQuery(''); setLimit(30); input.current?.focus() }}><svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m6 6 8 8M14 6l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg></button>}
    </div>
    <p className="trip-search-scope" id="trip-search-scope">筛选当前范围内的可选设施，支持简称和入口名。</p>
    {allowAuto && <button type="button" className={`trip-auto-choice${!current ? ' chosen' : ''}`} aria-pressed={!current} onClick={() => onChoose(null)}><div><strong>自动推荐</strong><small>规划时按全程步行距离选择</small></div><span aria-hidden="true">{!current ? '✓' : '›'}</span></button>}
    <div className="trip-picker-count" role="status" aria-live="polite">{loading ? '正在读取可选设施…' : `${groups.length} 处设施 · 来自当前检索结果`}</div>
    {error && <div className="trip-inline-notice" role="alert"><p>{error}</p><button type="button" onClick={onRetry}>重新读取设施</button></div>}
    {!loading && !error && !groups.length && <div className="trip-search-empty"><strong>{query.trim() ? '当前列表没有匹配的设施' : '这一类暂时没有可选设施'}</strong><p>未检索到、已失效或入口受阻的设施不会列入。{category === '生鲜采买' ? '售菜待确认的门店也不会自动列入。' : '可以缩短名称重试，或补录遗漏设施。'}</p><button type="button" onClick={onAddMissing}>补录遗漏设施 ↗</button></div>}
    {category === '生鲜采买' && <div className="trip-lookup">
      <div><strong>找不到想去的门店？</strong><p>{canLookup ? '按店名补查当前分析范围，点击后才发起设施检索。' : '模拟结果仅支持筛选已有设施。'}</p></div>
      <button type="button" disabled={!canLookup || loading || Boolean(error) || lookupBusy || query.trim().length < 2} onClick={() => onLookup(query.trim())}>{lookupBusy ? '补查中…' : '按店名补查'}</button>
    </div>}
    {lookupError && <p className="trip-blocked" role="alert">{lookupError} 原列表已保留。</p>}
    {lookupWarnings.length > 0 && <div className="trip-lookup-result" role="status">{lookupWarnings.map(warning => <p key={warning}>{warning}</p>)}</div>}
    <div className="trip-option-list">{groups.slice(0, limit).map(group => {
      const option = group[0], chosen = group.some(o => o.entry_id === current?.entry_id)
      return <article className={`trip-facility-option${chosen ? ' chosen' : ''}`} key={option.place_id}>
        <div className="trip-option-head"><div><strong>{option.name}</strong><small>距起点直线 {tripMeters(Math.min(...group.map(o => o.straight_m)))}{option.in_circle ? ' · 圈内' : ''}</small></div>{chosen && <span className="trip-choice-badge">已选择</span>}</div>
        {option.fresh_status && <p className="trip-option-evidence">{freshLabel(option.fresh_status)}</p>}
        {group.length === 1 ? <button type="button" className="trip-option-select" aria-pressed={chosen} onClick={() => onChoose(option)}>{option.gate ? `选择 · ${option.gate}` : '选择这家'}<span aria-hidden="true">{chosen ? '✓' : '›'}</span></button> : <div className="trip-entry-options" aria-label={`${option.name}的可选入口`}>{group.map(entry => <button type="button" key={entry.entry_id} aria-pressed={entry.entry_id === current?.entry_id} onClick={() => onChoose(entry)}>{entry.gate ?? (entry.distance_basis === 'navigation_point' ? '导航入口' : '设施位置')}{entry.entry_id === current?.entry_id ? ' ✓' : ''}</button>)}</div>}
      </article>
    })}</div>
    {groups.length > limit && <button type="button" className="trip-secondary-action" onClick={() => setLimit(value => value + 30)}>显示更多设施</button>}
    <p className="trip-note">列表按距起点的直线距离排序。{allowAuto ? '规划后显示实际步行距离。' : '更换后重新计算相邻路段，其他站点和入口保持不变。'}</p>
  </section>
}
