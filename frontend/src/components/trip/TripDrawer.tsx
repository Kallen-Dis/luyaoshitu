import { useCallback, useEffect, useEffectEvent, useMemo, useRef, useState } from 'react'
import { ApiError, tripNearest, tripOptions, tripPlan } from '../../api'
import type { IsochroneFeature, TripItem, TripMapData, TripNearestResult, TripOptionsResult, TripOrigin, TripPlanResult, TripStopOption } from '../../types'
import { PLACE_MARK } from '../map/context'
import { tripMeters, tripMinutes, tripVerification } from '../../lib/trip'
import { TripCategoryPicker } from './TripCategoryPicker'
import { TripNearestList } from './TripNearestList'
import { TripItinerary } from './TripItinerary'
import { TripPlanEditor } from './TripPlanEditor'
import { TripStopPicker } from './TripStopPicker'
import { freshLabel } from '../../lib/fresh'
import { FreshFeedback } from './FreshFeedback'
import { itineraryActions, itineraryGuideIssue, itineraryLegIssue, itineraryRetryIssue, originLabel, planRequestFor, retryRestriction, tripDraftIssue, tripDraftPins, tripReplacementIssue, tripUpdateIssue, type ItineraryAction, type PlanOperation } from '../../lib/itinerary'
import './trip.css'

interface Props {
  feature: IsochroneFeature; origin: TripOrigin; initialCategory?: string; targetId?: string
  selected: string | null; selectionKey: number; picking: boolean
  onSelect: (id: string | null) => void; onMapData: (data: TripMapData) => void
  onClose: () => void; onPickOrigin: () => void; onResetOrigin: () => void; onOriginChange: (origin: TripOrigin) => void; onCancelPick: () => void
  hidden?: boolean
  onGuide: (item: TripItem, scope?: 'journey' | 'segment') => void
  onAddMissing: (category?: string) => void
}
type NearestOperation = { kind: 'nearest'; key: string; origin: TripOrigin; category: string; target?: string; includePending: boolean; query: string }
type Operation = NearestOperation | PlanOperation
type Failure = { message: string; operation: Operation; canRetry: boolean }
const NO_ITEMS: TripItem[] = []

export function TripDrawer({ feature, origin, initialCategory, targetId, selected, selectionKey, picking, onSelect, onMapData, onClose, onPickOrigin, onResetOrigin, onOriginChange, onCancelPick, onAddMissing, onGuide, hidden = false }: Props) {
  const [includePending, setIncludePending] = useState(false)
  const [shopQuery, setShopQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [mode, setMode] = useState<'nearest' | 'plan'>('nearest')
  const [category, setCategory] = useState<string | null>(initialCategory ?? null)
  const failed = feature.properties.coverage?.failed_categories ?? []
  const [stops, setStops] = useState<string[]>(() => Object.keys(PLACE_MARK).filter(c => !failed.includes(c) && (c === '生鲜采买' || c === '基础教育')))
  const [destinations, setDestinations] = useState<Record<string, TripStopOption>>({})
  const [picker, setPicker] = useState<{ index: number; category: string } | null>(null)
  const [optionsVersion, setOptionsVersion] = useState(0)
  const [optionsEntry, setOptionsEntry] = useState<{ owner: IsochroneFeature; key: string; value?: TripOptionsResult; error?: string } | null>(null)
  const [lookupEntry, setLookupEntry] = useState<{ owner: IsochroneFeature; key: string; busy: boolean; error?: string; warnings?: string[] } | null>(null)
  const lookupController = useRef<AbortController | null>(null)
  const lookupSequence = useRef(0)
  const pickerReturn = useRef<number | null>(null)
  const [nearestEntry, setNearest] = useState<{ owner: IsochroneFeature; key: string; value: TripNearestResult } | null>(null)
  const [planEntry, setPlan] = useState<{ owner: IsochroneFeature; origin: TripOrigin; value: TripPlanResult } | null>(null)
  const [editor, setEditor] = useState(false)
  const [working, setWorking] = useState<Operation | null>(null)
  const [failure, setFailure] = useState<Failure | null>(null)
  const [fitKey, setFitKey] = useState(0)
  const heading = useRef<HTMLHeadingElement>(null)
  const scroll = useRef<HTMLDivElement>(null)
  const overviewScroll = useRef(0)
  const returnSelection = useRef<string | null>(null)
  const latestSelection = useRef(selected)
  const requestId = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const busy = working !== null
  const target = targetId && category === initialCategory && mode === 'nearest' ? targetId : undefined
  const nearestKey = JSON.stringify([origin, category, target, includePending, submittedQuery])
  const nearest = nearestEntry?.owner === feature && nearestEntry.key === nearestKey ? nearestEntry.value : null
  const plan = planEntry?.owner === feature ? planEntry.value : null
  const editing = mode === 'plan' && (editor || !plan)
  const result = mode === 'nearest' ? nearest : plan
  const planOrigin = planEntry?.owner === feature ? planEntry.origin : origin
  const mapOrigin = mode === 'plan' && !editing ? planOrigin : origin
  const items = useMemo(() => mode === 'nearest' ? nearest?.items ?? NO_ITEMS : editing ? NO_ITEMS : plan?.legs ?? NO_ITEMS, [mode, nearest, editing, plan])
  const activeIndex = plan?.legs.findIndex(leg => leg.entry_id === selected) ?? -1
  const active = activeIndex >= 0 ? plan?.legs[activeIndex] : undefined
  const simulated = Boolean(feature.properties.simulated || result?.preview)
  const pickerOrigin = editing ? origin : planOrigin
  const optionsKey = JSON.stringify([pickerOrigin, optionsVersion])
  const options = optionsEntry?.owner === feature && optionsEntry.key === optionsKey ? optionsEntry : null
  const optionsReady = Boolean(options?.value || options?.error)
  const pickerOpen = picker !== null
  const pickerGroup = options?.value?.groups.find(group => group.category === picker?.category)
  const lookupKey = JSON.stringify([optionsKey, picker])
  const lookup = lookupEntry?.owner === feature && lookupEntry.key === lookupKey ? lookupEntry : null

  useEffect(() => () => { lookupController.current?.abort(); lookupSequence.current++ }, [feature, lookupKey])

  const lookupShop = async (query: string) => {
    if (simulated || picker?.category !== '生鲜采买' || !options?.value || lookup?.busy || query.length < 2 || query.length > 60) return
    lookupController.current?.abort()
    const ctrl = new AbortController(), sequence = ++lookupSequence.current
    lookupController.current = ctrl
    setLookupEntry({ owner: feature, key: lookupKey, busy: true })
    try {
      const value = await tripOptions({ feature, origin: pickerOrigin, categories: ['生鲜采买'], poi_query: query }, ctrl.signal)
      if (ctrl.signal.aborted || sequence !== lookupSequence.current) return
      setOptionsEntry(previous => previous?.owner === feature && previous.key === optionsKey && previous.value
        ? { ...previous, value: { ...previous.value, groups: previous.value.groups.map(group => value.groups.find(next => next.category === group.category) ?? group) } } : previous)
      setLookupEntry({ owner: feature, key: lookupKey, busy: false, warnings: value.warnings?.filter(warning => !warning.includes('原快照的网格判定')) ?? [] })
    } catch (err) {
      if (!ctrl.signal.aborted && sequence === lookupSequence.current) setLookupEntry({ owner: feature, key: lookupKey, busy: false, error: err instanceof Error ? err.message : '门店补查失败，请稍后重试。' })
    }
  }

  useEffect(() => {
    if (!pickerOpen || optionsReady) return
    const ctrl = new AbortController()
    tripOptions({ feature, origin: pickerOrigin, categories: Object.keys(PLACE_MARK) }, ctrl.signal)
      .then(value => { if (!ctrl.signal.aborted) setOptionsEntry({ owner: feature, key: optionsKey, value }) })
      .catch((err: Error) => { if (!ctrl.signal.aborted) setOptionsEntry({ owner: feature, key: optionsKey, error: err.message }) })
    return () => ctrl.abort()
  }, [feature, pickerOpen, optionsReady, pickerOrigin, optionsKey])
  useEffect(() => {
    if (picker || pickerReturn.current === null) return
    scroll.current?.querySelector<HTMLButtonElement>(`[data-destination-index="${pickerReturn.current}"]`)?.focus()
    pickerReturn.current = null
  }, [picker])

  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    heading.current?.focus()
    return () => { controller.current?.abort(); trigger?.focus() }
  }, [])
  useEffect(() => {
    if (hidden) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); if (picker) setPicker(null); else onClose() } }
    document.addEventListener('keydown', close)
    return () => document.removeEventListener('keydown', close)
  }, [onClose, hidden, picker])
  useEffect(() => { if (scroll.current) scroll.current.scrollTop = !picker && mode === 'plan' && !selected && !editing ? overviewScroll.current : 0 }, [selected, editing, mode, picker])
  useEffect(() => { latestSelection.current = selected }, [selected])

  const cancelRequest = useCallback(() => { controller.current?.abort(); requestId.current++; setWorking(null) }, [])
  const run = useCallback(async (operation: Operation) => {
    controller.current?.abort()
    const ctrl = new AbortController(); controller.current = ctrl
    const id = ++requestId.current
    setWorking(operation); setFailure(null)
    try {
      if (operation.kind === 'nearest') {
        const value = await tripNearest({ feature, origin: operation.origin, category: operation.category, limit: 5, target_place_id: operation.target, include_pending: operation.includePending, poi_query: operation.query || undefined }, ctrl.signal)
        if (ctrl.signal.aborted || id !== requestId.current) return
        setNearest({ owner: feature, key: operation.key, value }); onSelect(value.items[0]?.entry_id ?? null)
      } else {
        const value = await tripPlan({ feature, ...planRequestFor(operation) }, ctrl.signal)
        if (ctrl.signal.aborted || id !== requestId.current) return
        if (operation.kind === 'replace') {
          const issue = tripReplacementIssue(operation.plan, value, operation.index, operation.selection)
          if (issue) throw new Error(issue)
        }
        if (operation.kind === 'create') {
          const issue = tripDraftIssue(operation.stops, operation.fixed ?? [], value) ?? tripUpdateIssue(operation.previous, value)
          if (issue) throw new Error(issue)
        }
        if (operation.kind === 'retry' && (value.stops.join('\0') !== operation.plan.stops.join('\0') || value.legs.length !== operation.plan.legs.length || value.legs.some((leg, i) => leg.entry_id !== operation.plan.legs[i].entry_id || leg.place_id !== operation.plan.legs[i].place_id))) throw new Error('未能保留原站点与入口，原行程已保留。')
        const next = operation.kind === 'retry' ? { ...value, alternatives: operation.plan.alternatives } : value
        setPlan({ owner: feature, origin: { ...operation.origin }, value: next })
        setStops([...next.stops]); setDestinations(Object.fromEntries(next.legs.map(leg => [leg.category, leg]))); setEditor(false)
        if (operation.kind === 'create') overviewScroll.current = 0
        onSelect(operation.kind === 'replace' ? next.legs[operation.index]?.entry_id ?? null : operation.kind === 'create' ? null : latestSelection.current)
        setFitKey(key => key + 1)
      }
    } catch (err) {
      if (!ctrl.signal.aborted && id === requestId.current) {
        const message = err instanceof Error ? err.message : String(err)
        setFailure({ message, operation, canRetry: !(err instanceof ApiError && (err.httpStatus === 429 || err.httpStatus === 400)) && !retryRestriction(message) })
      }
    } finally { if (!ctrl.signal.aborted && id === requestId.current) setWorking(null) }
  }, [feature, onSelect])

  const nearestOperation = (): NearestOperation | null => category ? { kind: 'nearest', key: nearestKey, origin: { ...origin }, category, target, includePending: category === '生鲜采买' && includePending, query: category === '生鲜采买' ? submittedQuery : '' } : null
  const autoNearest = useEffectEvent(() => {
    cancelRequest(); setFailure(null)
    if (mode === 'nearest' && !picking) { const operation = nearestOperation(); if (operation) void run(operation) }
  })
  // Only nearest queries are automatic. Editing an itinerary never starts a paid calculation.
  useEffect(() => {
    let live = true
    queueMicrotask(() => { if (live) autoNearest() })
    return () => { live = false; controller.current?.abort() }
  }, [feature, nearestKey, mode, picking])
  useEffect(() => {
    onMapData({ origin: mapOrigin, items, selected: editing ? null : selected, hover: null, fitKey, selectionKey, itinerary: mode === 'plan', preview: simulated, editing })
  }, [mapOrigin, items, selected, fitKey, selectionKey, mode, simulated, editing, onMapData])

  const overview = () => { onSelect(null); setFitKey(key => key + 1) }
  const chooseMode = (next: 'nearest' | 'plan') => {
    if (next === mode) return
    cancelRequest(); setFailure(null); setPicker(null); onCancelPick(); onSelect(null); setMode(next)
    if (next === 'plan') { setEditor(!plan); if (plan) { setStops([...plan.stops]); onOriginChange(planOrigin) } }
  }
  const edit = () => { cancelRequest(); setFailure(null); returnSelection.current = selected; setStops(plan ? [...plan.stops] : stops); if (plan) setDestinations(Object.fromEntries(plan.legs.map(leg => [leg.category, leg]))); onOriginChange(planOrigin); setEditor(true); onSelect(null) }
  const cancelEdit = () => {
    cancelRequest(); setFailure(null); onCancelPick()
    if (plan) { setStops([...plan.stops]); setDestinations(Object.fromEntries(plan.legs.map(leg => [leg.category, leg]))); onOriginChange(planOrigin); setEditor(false); onSelect(plan.legs.some(leg => leg.entry_id === returnSelection.current) ? returnSelection.current : null) }
  }
  const updateStops = (next: string[]) => { setFailure(null); setStops(next); setDestinations(current => Object.fromEntries(Object.entries(current).filter(([category]) => next.includes(category)))) }
  const submitPlan = () => { if (stops.length && !picking) void run({ kind: 'create', origin: { ...origin }, stops: [...stops], fixed: tripDraftPins(stops, destinations), previous: plan ?? undefined }) }
  const pickDestination = (index: number) => { setFailure(null); pickerReturn.current = index; setPicker({ index, category: editing ? stops[index] : plan!.stops[index] }) }
  const chooseDestination = (option: TripStopOption | null) => {
    if (!picker) return
    if (editing) setDestinations(current => {
      const next = { ...current }; if (option) next[picker.category] = option; else delete next[picker.category]; return next
    })
    else if (plan && option && option.entry_id !== plan.legs[picker.index]?.entry_id) void run({ kind: 'replace', origin: { ...planOrigin }, plan, index: picker.index, selection: { place_id: option.place_id, entry_id: option.entry_id } })
    setPicker(null)
  }
  const actions = plan ? itineraryActions(plan, selected, failure && !failure.canRetry ? failure.message : null) : null
  const perform = (action: ItineraryAction) => {
    if (action.kind === 'edit') edit()
    else if (action.kind === 'retry' && plan) void run({ kind: 'retry', origin: { ...planOrigin }, plan })
    else if (action.item) onGuide(action.item, action.kind === 'journey' ? 'journey' : 'segment')
  }
  const canContinue = Boolean(failure?.operation.kind === 'replace' && plan && !itineraryGuideIssue(plan))
  const recoverHint = plan ? itineraryRetryIssue(plan) : null
  const title = picker ? '选择目的地' : mode === 'nearest' ? '出行规划' : !editing && active ? `第 ${activeIndex + 1} 段` : '顺路去几处'
  const notice = failure?.message ?? (!editing && plan ? actions?.issue : null)
  const knownCount = plan?.legs.filter(leg => !itineraryLegIssue(leg, simulated)).length ?? 0

  return <aside hidden={hidden} className={`trip-drawer floating${mode === 'plan' ? ' trip-planning' : ''}${editing ? ' trip-editing' : ''}`} role="dialog" aria-labelledby="trip-title" aria-modal="false" aria-busy={busy}>
    <header className="trip-head"><div>{mode === 'plan' && !editing && active && !picker && <button type="button" className="trip-back" onClick={overview}>← 整个行程</button>}<h2 id="trip-title" ref={heading} tabIndex={-1}>{title}</h2><p>{picker ? `第 ${picker.index + 1} 站 · ${picker.category}` : mode === 'plan' ? editing ? '先安排站点，再规划路线' : active ? `第 ${activeIndex || '起点'}${activeIndex ? ' 站' : ''} → 第 ${activeIndex + 1} 站` : '按你的顺序步行' : '从起点查找附近设施'}</p></div>
      {mode === 'plan' && !editing && !picker && <button type="button" className="trip-edit-link" onClick={edit} disabled={busy}>编辑行程</button>}<button type="button" className="trip-close" aria-label="关闭出行规划" onClick={onClose}>×</button>
    </header>
    <div className="trip-scroll" ref={scroll} onScroll={event => { if (mode === 'plan' && !selected && !editing && !picker) overviewScroll.current = event.currentTarget.scrollTop }}>
      {picker ? <TripStopPicker index={picker.index} category={picker.category} options={pickerGroup?.items ?? []}
        current={editing ? destinations[picker.category] : plan?.legs[picker.index]} loading={!optionsReady} error={options?.error ?? pickerGroup?.error}
        allowAuto={editing} canLookup={!simulated} lookupBusy={Boolean(lookup?.busy)} lookupError={lookup?.error} lookupWarnings={lookup?.warnings ?? []}
        onLookup={query => void lookupShop(query)} onAddMissing={() => onAddMissing(picker.category)}
        onChoose={chooseDestination} onBack={() => setPicker(null)} onRetry={() => setOptionsVersion(version => version + 1)} /> : <>
      {(mode === 'nearest' || editing) && <div className="trip-modes" role="group" aria-label="出行方式"><button type="button" aria-pressed={mode === 'nearest'} onClick={() => chooseMode('nearest')}>找最近的</button><button type="button" aria-pressed={mode === 'plan'} onClick={() => chooseMode('plan')}>顺路去几处</button></div>}
      {mode === 'plan' && editing ? <TripPlanEditor origin={origin} stops={stops} destinations={destinations} failed={failed} busy={busy} picking={picking} onStops={updateStops} onPickDestination={pickDestination} onPickOrigin={() => { setFailure(null); onPickOrigin() }} onResetOrigin={() => { setFailure(null); onResetOrigin() }} /> : mode === 'nearest' && <>
        <details className="trip-settings" open={!nearest || picking}><summary>起点和设施类别<span>{originLabel(origin)}</span></summary>
          <div className="trip-origin"><b>起</b><span>{originLabel(origin)}</span></div>
          <div className="trip-origin-actions"><button type="button" onClick={onPickOrigin} disabled={busy}>在地图上换起点</button>{origin.kind !== 'center' && <button type="button" onClick={onResetOrigin} disabled={busy}>回到分析中心</button>}</div>
          <TripCategoryPicker value={category} failed={failed} disabled={busy} onChange={setCategory} />
          {category === '生鲜采买' && <div className="trip-fresh-tools">
            <label className="trip-pending-toggle"><input type="checkbox" checked={includePending} disabled={busy} onChange={event => setIncludePending(event.target.checked)} />比较是否卖菜待确认的超市</label>
            <form className="trip-shop-search" onSubmit={event => { event.preventDefault(); const query = shopQuery.trim(); if (query.length >= 2) { if (query === submittedQuery) { const operation = nearestOperation(); if (operation) void run(operation) } else setSubmittedQuery(query) } }}>
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5" stroke="currentColor" strokeWidth="1.7" /><path d="m16 16 4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" /></svg>
              <input aria-label="查找遗漏的买菜门店" value={shopQuery} maxLength={60} placeholder="输入遗漏门店名称" onChange={event => setShopQuery(event.target.value)} /><button type="submit" disabled={busy || shopQuery.trim().length < 2}>补查</button>
            </form>
          </div>}
        </details>
      </>}
      <div className="trip-status" aria-live="polite" aria-atomic="true">{busy ? working?.kind === 'replace' ? `正在更新第 ${working.index + 1} 站…` : working?.kind === 'retry' ? '正在补取所选行程的路线…' : '正在按路网计算…' : mode === 'nearest' ? picking ? '请在地图上点选起点。' : !category ? '选择一类设施，查看步行距离。' : nearest ? `找到 ${nearest.items.length} 家可比较设施。` : '' : ''}</div>
      {mode === 'plan' && !editing && plan && <div className="trip-journey-metric"><strong>{(active?.walk_m ?? plan.total_m) >= 1000 ? `${((active?.walk_m ?? plan.total_m) / 1000).toFixed(2)}` : Math.round(active?.walk_m ?? plan.total_m)}<span>{(active?.walk_m ?? plan.total_m) >= 1000 ? '公里' : '米'}</span></strong><p>约 {tripMinutes(active?.duration_s ?? plan.total_s)}{!active ? ` · ${plan.legs.length} 站` : ''}{plan.basis !== 'network' ? ' · 估算' : ''}</p>{active && <small>全程 {tripMeters(plan.total_m)} · 约 {tripMinutes(plan.total_s)}</small>}</div>}
      {notice && <div className={`trip-inline-notice${plan?.routing_status === 'blocked' ? ' blocked' : ''}`} role="alert"><strong>{notice}</strong>{canContinue ? <p>原行程已保留，可以继续查看或开始步行。</p> : !editing && plan && knownCount > 0 ? <p>{knownCount} 段路线已取得，可以单独预览。</p> : null}{failure && failure.canRetry && !busy && <button type="button" onClick={() => void run(failure.operation)}>{failure.operation.kind === 'replace' ? `重试更换第 ${failure.operation.index + 1} 站` : '重试本次操作'}</button>}</div>}
      {!editing && recoverHint && <p className="trip-note">{recoverHint}</p>}
      {result?.preview && <p className="trip-preview">验收模拟数据，距离与路线不代表真实路网。</p>}
      {mode === 'plan' && !editing && plan && <>
        {plan.legs.length ? <TripItinerary result={plan} selected={selected} busy={busy} originName={originLabel(planOrigin)} onSelect={onSelect} onGuide={item => onGuide(item, 'segment')} onChangeStop={pickDestination} /> : <p className="trip-empty">没有组成完整行程，请调整站点或起点。</p>}
        {!actions?.issue && !active && <p className="trip-route-status"><span aria-hidden="true">✓</span>{plan.legs.length} 段路线均已取得</p>}
      </>}
      {mode === 'nearest' && nearest && <>
        <div className="trip-results-head"><h3>{nearest.include_pending ? '附近设施（含待确认）' : '附近设施'}</h3><button type="button" onClick={overview}>查看全部路线</button></div>
        <TripNearestList items={nearest.items} selected={selected} estimate={nearest.basis !== 'network'} onSelect={onSelect} onGuide={onGuide} preview={simulated} />
        {(nearest.pending_count ?? 0) > 0 && <details className="trip-pending-list"><summary>是否卖菜待确认 · {nearest.pending_count} 家</summary><p className="trip-note">以下按直线距离列出候选；勾选上方比较开关后测步行距离。</p>
          {nearest.pending_candidates?.map(place => <div key={place.place_id}><b>{place.name}</b><small>{freshLabel(place.fresh_status)} · 直线 {tripMeters(place.straight_m)}</small><p>{place.fresh_evidence}</p><FreshFeedback place={place} disabled={simulated} /></div>)}
        </details>}
      </>}
      {!editing && <details className="trip-data-notes"><summary>路线来源与说明</summary><p className="trip-note">{result ? tripVerification(result) : '排名覆盖已检索设施。'}{mode === 'plan' && plan?.optimality === 'manual' ? ' · 固定所选站点与入口' : ''}</p>
        {result?.warnings.map(warning => <p className="trip-note" key={warning}>{warning}</p>)}
        {result && <><p className="trip-note">本次测距 {result.quota.matrix_pairs} 对 · 矩阵 {result.quota.matrix_requests} 次 · 路线 {result.quota.route_requests} 次</p><p className="trip-note">缓存命中 {result.quota.cache_hits} 项 · 出行日预算剩余 {result.quota.remaining.day_pairs} 对</p><p className="trip-note">{result.cache_policy === 'memory' ? '本次起点只使用内存缓存，不保存行程历史。' : '分析中心测距缓存 30 天，不保存行程历史。'}</p></>}
        <div className="trip-missing"><span>地图遗漏了设施？</span><button type="button" onClick={() => onAddMissing(category ?? undefined)}>补录设施 ↗</button></div>
      </details>}
      </>}
    </div>
    {mode === 'plan' && <footer className="trip-footer trip-plan-footer">
      {picker ? <><p>{editing ? '浏览和选择设施不会计算路线，返回行程后统一规划。' : '选定后更新相邻路段，其他站点与入口保持不变。'}</p><button type="button" className="trip-secondary-action" onClick={() => setPicker(null)}>{editing ? '返回设置行程' : '返回原行程'}</button></> : busy ? <><p>正在计算，请稍候。</p><button type="button" className="trip-secondary-action" onClick={cancelRequest}>取消本次计算</button></> : editing ? <>
        <p>{picking ? '先在地图上点选起点，再规划路线。' : stops.length ? '设置完成后，再计算路线。' : '请添加至少一站。'}</p>
        {failure && !failure.canRetry ? <p className="trip-note">{failure.message}</p> : <button type="button" className="trip-primary-action" disabled={!stops.length || picking} onClick={submitPlan}>{plan ? '更新路线' : '规划步行路线'}</button>}
        {plan && <button type="button" className="trip-secondary-action" onClick={cancelEdit}>取消编辑，返回原行程</button>}
      </> : plan && actions && <>
        <p>{canContinue ? '原行程从第 1 站开始。' : actions.primary.kind === 'journey' ? `先前往第 1 站：${plan.legs[0].name}` : actions.primary.kind === 'segment' ? `本段从${plan.legs.indexOf(actions.primary.item!) ? '上一站' : originLabel(planOrigin)}出发；全程仍从起点开始。` : '取得完整路线后，再开始整个行程。'}</p>
        <button type="button" className="trip-primary-action" onClick={() => canContinue ? onGuide(plan.legs[0], 'journey') : perform(actions.primary)}>{canContinue ? '继续原行程' : actions.primary.label}</button>
        {!canContinue && actions.secondary && <button type="button" className="trip-secondary-action" onClick={() => perform(actions.secondary!)}>{actions.secondary.label}</button>}
      </>}
    </footer>}
  </aside>
}
