import { useEffect, useRef, useState } from 'react'
import type { IsochroneFeature, TripGuideData, TripItem, TripNearbyResult, TripResult } from '../../types'
import { acceptReanchor, departureDistance, gpsToBaidu, locationQuality, observeGps, updateDeviation, guideUnavailableReason, type Deviation } from '../../lib/guide'
import { tripGuideNearby, tripReanchor } from '../../api'
import { tripMeters, tripMinutes } from '../../lib/trip'
import './guide.css'

interface Props {
  value: TripGuideData
  onChange: (next: TripGuideData) => void
  onClose: () => void
}

export function TripGuide({ value, onChange, onClose }: Props) {
  const { item, stepIndex, location } = value
  const steps = item.route?.steps ?? []
  const step = steps[stepIndex]
  const heading = useRef<HTMLHeadingElement>(null)
  const watch = useRef<(() => void) | null>(null)
  const latest = useRef({ value, onChange })
  const [locating, setLocating] = useState(false)
  const [positionError, setPositionError] = useState<string | null>(null)
  const [clock, setClock] = useState(() => Date.now())
  const [busy, setBusy] = useState<'reanchor' | 'nearby' | null>(null)
  const [requestError, setRequestError] = useState<string | null>(null)
  const [nearby, setNearby] = useState<TripNearbyResult | null>(null)
  const [decisionDismissed, setDecisionDismissed] = useState(false)
  const [manualDecision, setManualDecision] = useState(false)
  const [deviation, setDeviation] = useState<Deviation | null>(null)
  const [usage, setUsage] = useState<TripResult['quota'] | null>(null)
  const request = useRef<AbortController | null>(null)
  const requestSequence = useRef(0)
  useEffect(() => { latest.current = { value, onChange } })
  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    heading.current?.focus()
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); onClose() } }
    document.addEventListener('keydown', escape)
    return () => {
      watch.current?.()
      watch.current = null
      request.current?.abort()
      requestSequence.current += 1
      document.removeEventListener('keydown', escape)
      requestAnimationFrame(() => { if (trigger?.isConnected) trigger.focus({ preventScroll: true }) })
    }
  }, [onClose])
  useEffect(() => {
    if (!locating) return
    const timer = window.setInterval(() => {
      const now = Date.now()
      setClock(now)
      const current = latest.current
      if (current.value.location && now - current.value.location.timestamp > 30000) {
        setPositionError('超过 30 秒未获得新定位，位置标记已隐藏；可重试定位。')
        current.onChange({ ...current.value, location: null })
      }
      const data = current.value
      setDeviation(previous => data.mode === 'walking' ? updateDeviation(previous, data.location, data.item.route?.path ?? [], `${data.item.entry_id}:${data.item.from.lat}:${data.item.from.lng}`, now) : null)
    }, 5000)
    return () => window.clearInterval(timer)
  }, [locating])
  const focus = (kind: TripGuideData['focus']) => onChange({ ...value, focus: kind, focusKey: value.focusKey + 1 })
  const stopLocation = () => {
    cancelRequest()
    watch.current?.()
    watch.current = null
    setLocating(false)
    setPositionError(null)
    onChange({ ...value, location: null, focus: 'origin', focusKey: value.focusKey + 1 })
  }
  const startLocation = () => {
    if (watch.current !== null && location) { focus('location'); return }
    watch.current?.()
    watch.current = null
    if (!navigator.geolocation) { setPositionError('此浏览器不支持定位，可继续查看路线起点。'); return }
    setPositionError(null); setLocating(true)
    focus('location')
    watch.current = observeGps(navigator.geolocation, position => {
      const point = gpsToBaidu(position.coords.latitude, position.coords.longitude)
      if (!point || !Number.isFinite(position.coords.accuracy) || position.coords.accuracy < 0) {
        setPositionError('当前位置不在支持范围内，可继续查看路线起点。')
        watch.current?.()
        watch.current = null
        setLocating(false)
        const current = latest.current
        current.onChange({ ...current.value, location: null, focus: 'origin', focusKey: current.value.focusKey + 1 })
        return
      }
      setPositionError(null)
      const current = latest.current
      const timestamp = Number.isFinite(position.timestamp) ? Math.min(position.timestamp, Date.now()) : Date.now()
      setClock(Date.now())
      const fix = { ...point, accuracy: position.coords.accuracy, timestamp }
      current.onChange({ ...current.value, location: fix })
      const data = current.value
      setDeviation(previous => data.mode === 'walking' ? updateDeviation(previous, fix, data.item.route?.path ?? [], `${data.item.entry_id}:${data.item.from.lat}:${data.item.from.lng}`, Date.now()) : null)
    }, error => {
      setPositionError(error.code === 1 ? '定位未获授权，仍可查看路线起点和步骤。' : error.code === 3 ? '定位超时，请重试；当前位置暂不可用。' : '暂时无法获取位置，请重试。')
      watch.current?.()
      watch.current = null
      setLocating(false)
      const current = latest.current
      current.onChange({ ...current.value, location: null, focus: 'origin', focusKey: current.value.focusKey + 1 })
    })
  }
  const move = (delta: number) => onChange({ ...value, stepIndex: stepIndex + delta, focus: 'step', focusKey: value.focusKey + 1 })
  const stale = location && clock - location.timestamp > 30000
  const quality = locationQuality(location, clock)
  const distance = location ? departureDistance(location, item.from) : null
  const offRoute = value.mode === 'walking' && !quality && deviation?.confirmed && deviation.key === `${item.entry_id}:${item.from.lat}:${item.from.lng}`
  const cancelRequest = () => {
    requestSequence.current += 1; request.current?.abort(); setBusy(null); setRequestError(null)
  }
  const replan = async (replacement?: TripItem, feature?: IsochroneFeature) => {
    const current = latest.current
    const fix = current.value.location
    const issue = locationQuality(fix, Date.now())
    if (issue || !fix) { setRequestError(issue); return }
    request.current?.abort()
    const ctrl = new AbortController(), sequence = ++requestSequence.current
    request.current = ctrl
    const expected = replacement ? [replacement, ...current.value.remaining.slice(1)] : current.value.remaining
    setBusy('reanchor'); setRequestError(null); setUsage(null)
    try {
      const result = await tripReanchor({ feature: feature ?? current.value.routingFeature, origin: { lat: fix.lat, lng: fix.lng, kind: 'map' }, stops: expected.map(leg => leg.category), selected_stops: expected.map(({ place_id, entry_id }) => ({ place_id, entry_id })), places: expected.flatMap(leg => leg.place ? [leg.place] : []) }, ctrl.signal)
      if (ctrl.signal.aborted || sequence !== requestSequence.current) return
      setUsage(result.quota)
      const next = acceptReanchor(latest.current.value, result, expected)
      latest.current.onChange(next)
      setNearby(null); setDeviation(null); setDecisionDismissed(false); setManualDecision(false)
    } catch (err) { if (!ctrl.signal.aborted && sequence === requestSequence.current) setRequestError(err instanceof Error ? err.message : '重新规划失败。') }
    finally { if (!ctrl.signal.aborted && sequence === requestSequence.current) setBusy(null) }
  }
  const findNearby = async () => {
    const current = latest.current, fix = current.value.location
    const issue = locationQuality(fix, Date.now())
    if (issue || !fix) { setRequestError(issue); return }
    request.current?.abort()
    const ctrl = new AbortController(), sequence = ++requestSequence.current
    request.current = ctrl
    setBusy('nearby'); setRequestError(null); setUsage(null)
    try {
      const result = await tripGuideNearby({ feature: current.value.routingFeature, origin: { lat: fix.lat, lng: fix.lng, kind: 'map' }, category: current.value.item.category }, ctrl.signal)
      if (!ctrl.signal.aborted && sequence === requestSequence.current) { setNearby(result); setUsage(result.quota) }
    } catch (err) { if (!ctrl.signal.aborted && sequence === requestSequence.current) setRequestError(err instanceof Error ? err.message : '附近设施查询失败。') }
    finally { if (!ctrl.signal.aborted && sequence === requestSequence.current) setBusy(null) }
  }
  const nextStop = () => {
    if (busy || value.mode !== 'walking' || value.remaining.length < 2) return
    const remaining = value.remaining.slice(1)
    onChange({ ...value, remaining, item: remaining[0], stepIndex: 0, focus: 'origin', focusKey: value.focusKey + 1 })
    setDeviation(null); setRequestError(null); setDecisionDismissed(false); setManualDecision(false)
  }
  const showDecision = !nearby && !decisionDismissed && location && (value.mode === 'preview' || offRoute || manualDecision)
  return <section className="trip-guide" role="dialog" aria-modal="false" aria-labelledby="guide-title">
    <header className="guide-head">
      <div><small>{value.mode === 'walking' ? '步行中 · 已按出发位置重规划' : '沉浸步行引导 · 路线预览'}{value.remaining.length > 1 ? ` · 含后续 ${value.remaining.length - 1} 站` : ''}</small><h2 id="guide-title" ref={heading} tabIndex={-1} title={item.name}>{item.name}</h2></div>
      <button type="button" className="guide-exit" onClick={onClose}>退出引导 <span aria-hidden="true">×</span></button>
    </header>
    <div className="guide-map-actions" role="group" aria-label="引导地图视野">
      <button type="button" aria-pressed={value.focus === 'origin'} onClick={() => focus('origin')}>路线起点</button>
      <button type="button" aria-pressed={value.focus === 'route'} onClick={() => focus('route')}>全路线</button>
      <button type="button" aria-pressed={value.focus === 'location' && Boolean(location)} onClick={startLocation}>{locating && !location ? '定位中…' : '定位到我'}</button>
      {locating && <button type="button" onClick={stopLocation}>停止定位</button>}
      {location && !showDecision && !nearby && <button type="button" disabled={Boolean(busy)} onClick={() => { setDecisionDismissed(false); setManualDecision(true) }}>调整出发计划</button>}
    </div>
    <div className="guide-side">
    <div className="guide-position" role="status">{positionError ?? (location ? stale ? '上次定位 · 超过 30 秒未更新，请核对位置' : `当前位置 · 定位精度约 ${Math.round(location.accuracy)} 米${location.accuracy > 80 ? '，定位较粗' : ''}` : locating ? '正在获取当前位置…' : '未开启定位 · 地图显示路线起点')}{offRoute && <strong>持续偏离当前路线，请核对位置或重新规划。</strong>}</div>
    {showDecision && <div className="guide-departure">
      <b>{offRoute ? '调整当前路线' : distance?.kind === 'near' ? '你已在起点附近' : distance?.kind === 'far' ? '当前位置与起点不同' : '位置是否接近起点尚不确定'}</b>
      <p>{value.mode === 'preview' && distance ? `距计划起点约 ${Math.round(distance.meters)} 米。` : ''}{quality ?? '保留目的地和后续站序，按实际道路重新计算。'}</p>
      <button type="button" className="guide-primary" disabled={Boolean(quality || busy)} onClick={() => void replan()}>{distance?.kind === 'near' && value.mode === 'preview' ? '从这里开始步行' : '从当前位置前往原目的地'}</button>
      <button type="button" disabled={Boolean(quality || busy)} onClick={() => void findNearby()}>重选附近同类设施 · 最多 5 家</button>
      <button type="button" disabled={Boolean(busy)} onClick={() => { setDecisionDismissed(true); setRequestError(null) }}>{value.mode === 'walking' ? '保留当前路线' : '继续查看原计划'}</button>
      <small>选择出发或查附近，将使用本次定位调用百度路线或设施服务。</small>
    </div>}
    {busy && <div className="guide-request" role="status">{busy === 'reanchor' ? '正在核验新行程，当前仍展示原路线…' : '正在查询当前位置附近的设施…'}<button type="button" onClick={cancelRequest}>取消</button></div>}
    {requestError && <p className="guide-request-error" role="alert">{requestError} 原计划已保留。</p>}
    {usage && <p className="guide-usage">本次返回消耗：{usage.matrix_pairs} 个点对 · {usage.route_requests} 次路线 · {usage.poi_requests ?? 0} 次设施检索</p>}
    </div>
    {nearby ? <div className="guide-candidates">
      <header><div><b>当前位置附近的{item.category}</b><small>以本次查询起点测距 · {nearby.items.length} 家</small></div><button type="button" disabled={Boolean(busy)} onClick={() => setNearby(null)}>返回原计划</button></header>
      <div className="guide-candidate-scroll">
      {nearby.warnings.map(warning => <p className="guide-note" key={warning}>{warning}</p>)}
      {!nearby.items.length && <p>本次没有检索到可推荐的设施。原计划仍可查看。</p>}
      {nearby.items.map(candidate => <div className="guide-candidate" key={candidate.entry_id}><b>{candidate.rank} · {candidate.name}</b><span>{nearby.basis === 'network' ? '步行' : '估算'} {tripMeters(candidate.walk_m)} · 约 {tripMinutes(candidate.duration_s)}</span>{candidate.fresh_evidence && <small>{candidate.fresh_evidence}</small>}<button type="button" disabled={Boolean(busy || quality || guideUnavailableReason(candidate, nearby.preview))} onClick={() => void replan(candidate, nearby.routing_feature)}>{guideUnavailableReason(candidate, nearby.preview) ?? '选这家，从当前位置出发'}</button></div>)}
      </div><footer>选择后替换当前这一站，后续设施和顺序保持。距离排名仅覆盖已检索设施。</footer>
    </div> : <div className="guide-step-card">
      <div className="guide-step-main"><span className="guide-turn" aria-hidden="true">{step && /左/.test(step.instruction) ? '↰' : step && /右/.test(step.instruction) ? '↱' : step && /过马路|斑马线|人行横道/.test(step.instruction) ? '↔' : '↑'}</span><div><small>查看第 {stepIndex + 1} / {steps.length} 步</small><p>{step?.instruction ?? '沿所选路线步行'}</p><span>本步 {tripMeters(step?.distance_m ?? 0)} · 约 {tripMinutes(step?.duration_s ?? 0)}</span></div></div>
      <div className="guide-step-nav"><button type="button" disabled={Boolean(busy) || stepIndex === 0} onClick={() => move(-1)}>上一步</button><button type="button" onClick={() => focus('step')}>查看这一步</button><button type="button" disabled={Boolean(busy) || stepIndex >= steps.length - 1} onClick={() => move(1)}>下一步</button></div>
      <footer><b>{tripMeters(item.walk_m)} <span>· 约 {tripMinutes(item.duration_s)}</span></b><span>所选路段 · 手动查看步骤</span></footer>
      {item.route?.connectors.length ? <p className="guide-note">灰色虚线为端点连接，尚未核验通行。</p> : null}
      {locating && <p className="guide-note">定位和地图坐标换算可能有偏差，请结合实际道路查看。</p>}
      {value.remaining.length > 1 && <p className="guide-note">后续：{value.remaining.slice(1).map(leg => leg.name).join(' → ')}</p>}
      {value.mode === 'walking' && value.remaining.length > 1 && <button type="button" className="guide-next-stop" disabled={Boolean(busy)} onClick={nextStop}>已到达本站，继续下一站</button>}
    </div>}
  </section>
}
