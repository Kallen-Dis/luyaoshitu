import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState } from 'react'
import { tripNearest, tripPlan } from '../../api'
import type { IsochroneFeature, TripItem, TripMapData, TripNearestResult, TripOrigin, TripPlanResult, TripSelection } from '../../types'
import { PLACE_MARK, SHORT_NAME } from '../map/context'
import { tripMeters, tripMinutes, tripVerification } from '../../lib/trip'
import { TripCategoryPicker } from './TripCategoryPicker'
import { TripNearestList } from './TripNearestList'
import { TripItinerary } from './TripItinerary'
import { freshLabel } from '../../lib/fresh'
import { FreshFeedback } from './FreshFeedback'
import './trip.css'

interface Props {
  feature: IsochroneFeature; origin: TripOrigin; initialCategory?: string; targetId?: string
  selected: string | null; picking: boolean
  onSelect: (id: string | null) => void; onMapData: (data: TripMapData) => void
  onClose: () => void; onPickOrigin: () => void; onResetOrigin: () => void
  hidden?: boolean
  onGuide: (item: TripItem) => void
  onAddMissing: (category?: string) => void
}

export function TripDrawer({ feature, origin, initialCategory, targetId, selected, picking, onSelect, onMapData, onClose, onPickOrigin, onResetOrigin, onAddMissing, onGuide, hidden = false }: Props) {
  const [includePending, setIncludePending] = useState(false)
  const [shopQuery, setShopQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [mode, setMode] = useState<'nearest' | 'plan'>('nearest')
  const [category, setCategory] = useState<string | null>(initialCategory ?? null)
  const failed = feature.properties.coverage?.failed_categories ?? []
  const [stops, setStops] = useState<string[]>(() => Object.keys(PLACE_MARK).filter(c => !failed.includes(c)).filter(c => c === '生鲜采买' || c === '基础教育'))
  const [nearestEntry, setNearest] = useState<{ owner: IsochroneFeature; key: string; value: TripNearestResult } | null>(null)
  const [planEntry, setPlan] = useState<{ owner: IsochroneFeature; key: string; value: TripPlanResult } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hover, setHover] = useState<string | null>(null)
  const [fitKey, setFitKey] = useState(0)
  const heading = useRef<HTMLHeadingElement>(null)
  const requestId = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const target = targetId && category === initialCategory && mode === 'nearest' ? targetId : undefined
  const inputKey = JSON.stringify([origin, mode, category, stops, target, includePending, submittedQuery])
  const nearest = nearestEntry?.owner === feature && nearestEntry.key === inputKey ? nearestEntry.value : null
  const plan = planEntry?.owner === feature && planEntry.key === inputKey ? planEntry.value : null
  const result = mode === 'nearest' ? nearest : plan
  const items = useMemo(() => mode === 'nearest' ? nearest?.items ?? [] : plan?.legs ?? [], [mode, nearest, plan])

  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    heading.current?.focus()
    return () => { controller.current?.abort(); trigger?.focus() }
  }, [])
  useEffect(() => {
    if (hidden) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); onClose() } }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [onClose, hidden])

  const load = useCallback(async (replacement?: { index: number; selection: TripSelection }) => {
    if ((mode === 'nearest' && !category) || (mode === 'plan' && !stops.length)) return
    controller.current?.abort()
    const ctrl = new AbortController()
    controller.current = ctrl
    const id = ++requestId.current
    setBusy(true); setError(null)
    try {
      if (mode === 'nearest' && category) {
        const value = await tripNearest({ feature, origin, category, limit: 5, target_place_id: target, include_pending: category === '生鲜采买' && includePending, poi_query: category === '生鲜采买' ? submittedQuery || undefined : undefined }, ctrl.signal)
        if (ctrl.signal.aborted || id !== requestId.current) return
        setNearest({ owner: feature, key: inputKey, value }); onSelect(value.items[0]?.entry_id ?? null)
      } else {
        const value = await tripPlan({ feature, origin, stops,
          ...(replacement && plan ? { selected_stops: plan.legs.map(({ place_id, entry_id }) => ({ place_id, entry_id })), replace_stop: { index: replacement.index, ...replacement.selection } } : {})
        }, ctrl.signal)
        if (ctrl.signal.aborted || id !== requestId.current) return
        setPlan({ owner: feature, key: inputKey, value }); onSelect(value.legs[0]?.entry_id ?? null)
      }
    } catch (err) { if (!ctrl.signal.aborted && id === requestId.current) setError(err instanceof Error ? err.message : String(err)) }
    finally { if (!ctrl.signal.aborted && id === requestId.current) setBusy(false) }
  }, [mode, category, stops, feature, origin, target, onSelect, plan, inputKey, includePending, submittedQuery])
  // 自动请求仅由规划输入触发；plan 更新只供手动换站使用，不能触发再次请求。
  const loadCurrent = useEffectEvent(() => { void load() })
  useEffect(() => {
    let active = true
    onSelect(null)
    // 请求在 effect 完成后开始，忙碌状态属于网络生命周期。
    if (!picking) queueMicrotask(() => { if (active) loadCurrent() })
    return () => { active = false; controller.current?.abort() }
  }, [feature, origin, category, stops, mode, target, picking, onSelect, includePending, submittedQuery])

  useEffect(() => {
    onMapData({ origin, items, selected, hover, fitKey, itinerary: mode === 'plan', preview: result?.preview })
  }, [origin, items, mode, selected, hover, fitKey, onMapData, result?.preview])

  const reorder = (index: number, direction: number) => {
    const next = [...stops]; [next[index], next[index + direction]] = [next[index + direction], next[index]]; setStops(next)
  }
  return <aside hidden={hidden} className="trip-drawer floating" role="dialog" aria-labelledby="trip-title" aria-modal="false" aria-busy={busy}>
    <header className="trip-head"><h2 id="trip-title" ref={heading} tabIndex={-1}>出行规划</h2><button type="button" aria-label="关闭出行规划" onClick={onClose}>×</button></header>
    <div className="trip-scroll">
      <details className="trip-settings" open={!result || picking}><summary>修改起点和目的地 <span>{origin.kind === 'center' ? '分析中心' : '自选起点'} · {mode === 'nearest' ? category ? SHORT_NAME[category] : '选设施' : `${stops.length} 站`}</span></summary>
      <div className="trip-origin"><b>起</b><span>{origin.kind === 'center' ? '分析中心' : origin.kind === 'cell' ? '居民方格中心' : '地图选点'}<small>{origin.lat.toFixed(6)}, {origin.lng.toFixed(6)}</small></span></div>
      <div className="trip-origin-actions"><button type="button" onClick={onPickOrigin} disabled={busy}>在地图上换起点</button><button type="button" onClick={onResetOrigin} disabled={busy || origin.kind === 'center'}>回到分析中心</button></div>
      <div className="trip-modes" role="group" aria-label="出行方式"><button type="button" className={mode === 'nearest' ? 'pill active' : 'pill'} aria-pressed={mode === 'nearest'} onClick={() => setMode('nearest')} disabled={busy}>找最近的</button><button type="button" className={mode === 'plan' ? 'pill active' : 'pill'} aria-pressed={mode === 'plan'} onClick={() => setMode('plan')} disabled={busy}>顺路去几处</button></div>
      {mode === 'nearest' ? <TripCategoryPicker value={category} failed={failed} disabled={busy} onChange={setCategory} /> : <div className="trip-stop-editor">
        <p className="trip-note">按所选顺序走，最多 3 站。</p>
        {stops.map((stop, index) => <div key={stop}><span>{index + 1} · {SHORT_NAME[stop]}</span><button type="button" disabled={busy || index === 0} aria-label={`${SHORT_NAME[stop]}上移`} onClick={() => reorder(index, -1)}>↑</button><button type="button" disabled={busy || index === stops.length - 1} aria-label={`${SHORT_NAME[stop]}下移`} onClick={() => reorder(index, 1)}>↓</button><button type="button" disabled={busy} aria-label={`移除${SHORT_NAME[stop]}`} onClick={() => setStops(stops.filter(c => c !== stop))}>×</button></div>)}
        {stops.length < 3 && <select aria-label="添加下一站" value="" disabled={busy} onChange={event => setStops([...stops, event.target.value])}><option value="">+ 再加一站</option>{Object.keys(PLACE_MARK).filter(c => !failed.includes(c) && !stops.includes(c)).map(c => <option key={c} value={c}>{SHORT_NAME[c]}</option>)}</select>}
      </div>}
      {mode === 'nearest' && category === '生鲜采买' && <div className="trip-fresh-tools">
        <p className="trip-note">默认推荐已确认及规则推定的买菜门店。普通超市保留为待确认候选。</p>
        <label className="trip-pending-toggle"><input type="checkbox" checked={includePending} disabled={busy} onChange={event => setIncludePending(event.target.checked)} />将待确认超市加入距离比较</label>
        <form className="trip-shop-search" onSubmit={event => { event.preventDefault(); const query = shopQuery.trim(); if (query.length >= 2) { if (query === submittedQuery) void load(); else setSubmittedQuery(query) } }}>
          <input aria-label="查找遗漏的买菜门店" value={shopQuery} maxLength={60} placeholder="输入遗漏门店名称" onChange={event => setShopQuery(event.target.value)} />
          <button type="submit" disabled={busy || shopQuery.trim().length < 2}>补查</button>
        </form>
      </div>}
      </details>
      {error && <p className="trip-blocked" role="alert">{error} <button type="button" onClick={() => void load()}>重试</button></p>}
      <div aria-live="polite" aria-atomic="true" className="trip-status">{busy ? feature.properties.simulated ? '正在计算直线估算…' : '正在按真实路网测距…' : picking ? '请在地图上点选起点。' : result && items.length ? `找到 ${items.length} ${mode === 'nearest' ? '家' : '站'}，第一段${result.basis === 'network' ? '步行' : '估算'} ${tripMeters(items[0].walk_m)}，约 ${tripMinutes(items[0].duration_s)}。` : mode === 'nearest' && !category ? '选一类设施，看从起点走过去要多远。' : result ? '没有检索到可用的这一类设施。' : '选择要去的设施。'}</div>
      {result && <>
        {result.preview && <p className="trip-preview" role="alert">验收假数据 · 距离与折线均为模拟，请切换至正常开发入口 5173 使用真实路网。</p>}
        <p className="trip-basis">{result.preview ? '验收模拟距离' : result.basis === 'network' ? '路网测距' : '直线估算，未测路网'}</p>
        {result.routing_status && result.routing_status !== 'clear' && <p className="trip-warning">{result.routing_status === 'blocked' ? '指定行程受已知围挡影响，无法确认通行。' : '尚未找到已核验的可行路线；未核验候选没有作为畅通路线推荐。'}</p>}
        <p className="trip-note">{target ? '指定设施路线，按可用入口测距' : mode === 'plan' && plan?.optimality === 'manual' ? '手动选择，其他站点固定' : `${mode === 'nearest' ? '距离排名：' : ''}${tripVerification(result)}`}</p>
        {!target && nearest?.beyond_limit && mode === 'nearest' && nearest.basis === 'network' && <p className="trip-warning">当前最近结果也超过 1 公里；完整性或排序未确认时不能据此断言盲区。</p>}
        {result.warnings.filter(warning => /配额|预算|失败|上限/.test(warning)).map(warning => <p className="trip-warning" key={warning}>{warning}</p>)}
        {result.warnings.some(warning => !/配额|预算|失败|上限/.test(warning)) && <details className="trip-data-notes"><summary>检索范围与数据说明 · {result.warnings.filter(warning => !/配额|预算|失败|上限/.test(warning)).length} 条</summary>{result.warnings.filter(warning => !/配额|预算|失败|上限/.test(warning)).map(warning => <p className="trip-note" key={warning}>{warning}</p>)}</details>}
        {items.length > 0 && <div className="trip-results-head"><h3>{mode === 'nearest' ? nearest?.include_pending ? '附近设施（含待确认）' : '附近设施' : '行程路线'}<span>{items.length} {mode === 'nearest' ? '家' : '站'}</span></h3><button type="button" onClick={() => setFitKey(k => k + 1)}>查看全部路线</button></div>}
      </>}
      {mode === 'nearest' && nearest && <TripNearestList items={nearest.items} selected={selected} estimate={nearest.basis !== 'network'} onSelect={onSelect} onHover={setHover} onGuide={onGuide} preview={nearest.preview || feature.properties.simulated} />}
      {mode === 'nearest' && nearest && (nearest.pending_count ?? 0) > 0 && <details className="trip-pending-list">
        <summary>是否卖菜待确认的超市 · {nearest.pending_count} 家</summary>
        <p className="trip-note">以下按直线距离列出最近 {nearest.pending_candidates?.length ?? 0} 家候选，步行距离需勾选上方比较开关测量。</p>
        {nearest.pending_candidates?.map(place => <div key={place.place_id}><b>{place.name}</b><small>{freshLabel(place.fresh_status)} · 直线 {tripMeters(place.straight_m)}</small><p>{place.fresh_evidence}</p><FreshFeedback place={place} disabled={nearest.preview || feature.properties.simulated} /></div>)}
      </details>}
      {mode === 'plan' && plan && <TripItinerary result={plan} selected={selected} busy={busy} onSelect={onSelect} onGuide={onGuide} onReplace={(index, selection) => void load({ index, selection })} />}
      <div className="trip-missing"><span>地图上还有遗漏的设施？</span><button type="button" onClick={() => onAddMissing(category ?? undefined)}>补录设施 ↗</button></div>
    </div>
    <footer className="trip-footer"><p>{result?.preview ? '验收假数据 · 不代表真实路网' : feature.properties.simulated ? '离线估算 · 虚线尚未核验通行' : '百度路网测距 · 排名仅覆盖已检索设施'}</p>{result && <details><summary>配额与缓存说明</summary><p>本次测距 {result.quota.matrix_pairs} 对 · 矩阵 {result.quota.matrix_requests} 次 · 路线 {result.quota.route_requests} 次</p><p>缓存命中 {result.quota.cache_hits} 项 · 今日剩余 {result.quota.remaining.day_pairs} 对</p><p>{result.cache_policy === 'memory' ? '本次位置仅用内存缓存，不保存行程历史。' : '分析中心测距缓存 30 天，不保存行程历史。'}</p></details>}</footer>
  </aside>
}
