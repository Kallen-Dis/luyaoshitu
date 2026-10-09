import type { TripFixedStop, TripItem, TripMapData, TripOrigin, TripPlanResult, TripSelection } from '../types'

export type PlanOperation = {
  kind: 'create'; origin: TripOrigin; stops: string[]; previous?: TripPlanResult; fixed?: TripFixedStop[]
} | {
  kind: 'replace'; origin: TripOrigin; plan: TripPlanResult; index: number; selection: TripSelection
} | {
  kind: 'retry'; origin: TripOrigin; plan: TripPlanResult
}

export function originLabel(origin: TripOrigin) {
  return origin.kind === 'center' ? '分析中心' : origin.kind === 'cell' ? '居民方格中心' : '地图选点'
}

/** The failed operation, including its fixed gates, is the retry input; never read the current draft. */
export function planRequestFor(operation: PlanOperation) {
  if (operation.kind === 'create') return { origin: { ...operation.origin }, stops: [...operation.stops], ...(operation.fixed?.length ? { fixed_stops: operation.fixed.map(pin => ({ ...pin })) } : {}) }
  return {
    origin: { ...operation.origin }, stops: [...operation.plan.stops],
    selected_stops: operation.plan.legs.map(({ place_id, entry_id }) => ({ place_id, entry_id })),
    ...(operation.kind === 'replace' ? { replace_stop: { index: operation.index, ...operation.selection } } : { retry_routes: true }),
  }
}

export function itineraryLegIssue(item: TripItem, preview = false): string | null {
  if (preview) return '模拟路线不支持引导'
  if (item.closure_status === 'blocked') return '受已知围挡阻断'
  if (!item.route || item.route.path.length < 2) return '尚未取得步行路线'
  if (item.closure_status !== 'clear') return '通行情况待核验'
  if (!item.route.steps.length) return '尚未取得步行步骤'
  return null
}

export function retryRestriction(message: string): string | null {
  return /配额|小时.*(?:预算|上限|限流)|日.*预算.*(?:用完|耗尽)|请求过于频繁/.test(message) ? message : null
}

export function itineraryRetryIssue(plan: TripPlanResult): string | null {
  if (plan.preview || plan.basis === 'estimate') return '模拟结果不能补取真实路线'
  const remaining = plan.quota?.remaining
  if (remaining?.hour_routes === 0) return '出行小时路线预算已用完，请稍后再试。'
  if (plan.basis !== 'network' && remaining && (!remaining.day_pairs || !remaining.hour_pairs)) return '测距预算不足，请在额度恢复后重新规划。'
  return plan.legs.map(leg => leg.note && itineraryLegIssue(leg) ? retryRestriction(leg.note) : null).find(Boolean) ?? null
}

export function itineraryPath(item: TripItem, preview = false): [number, number][] {
  return preview ? [[item.from.lng, item.from.lat], [item.lng, item.lat]] : item.route?.path ?? []
}

export type TripViewStamp = Pick<TripMapData, 'items' | 'selected' | 'fitKey' | 'selectionKey' | 'itinerary'>

/** Hover and unrelated rerenders must never claim or move the camera. */
export function tripCameraRequest(previous: TripViewStamp | null, trip: TripMapData): { selected: string | null } | null {
  if (!trip.items.length) return null
  if (trip.itinerary && (!previous || !previous.itinerary || previous.items !== trip.items || previous.selected !== trip.selected || previous.selectionKey !== trip.selectionKey)) return { selected: trip.selected }
  if (trip.fitKey > 0 && trip.fitKey !== previous?.fitKey) return { selected: null }
  return null
}

export function tripViewBounds(trip: Pick<TripMapData, 'origin' | 'items'>, selected: string | null): [number, number][] {
  const items = selected ? trip.items.filter(item => item.entry_id === selected) : trip.items
  const points: [number, number][] = selected ? [] : [[trip.origin.lng, trip.origin.lat]]
  for (const item of items) points.push(
    [item.from.lng, item.from.lat], [item.lng, item.lat],
    ...(item.route?.path ?? []), ...(item.route?.blocked_path ?? []), ...(item.route?.connectors.flat() ?? []),
  )
  return points
}

export interface ViewRect { left: number; top: number; right: number; bottom: number; width: number; height: number }
export function tripDrawerIsBottom(map: ViewRect, drawer: ViewRect) {
  return drawer.width >= map.width * .65 && drawer.left - map.left <= 24 && drawer.right >= map.right - 24 && drawer.top - map.top >= map.height * .25
}
/** Use the actual side drawer or mobile bottom sheet, leaving a usable map rectangle. */
export function tripViewportMargins(map: ViewRect, drawer?: ViewRect | null, toolbar?: ViewRect | null): [number, number, number, number] {
  let top = 40, right = 36, bottom = 70
  if (toolbar && toolbar.bottom > map.top && toolbar.top < map.bottom) top = Math.max(top, toolbar.bottom - map.top + 18)
  if (drawer && drawer.left < map.right && drawer.right > map.left && drawer.top < map.bottom && drawer.bottom > map.top) {
    if (tripDrawerIsBottom(map, drawer)) bottom = Math.max(bottom, map.bottom - drawer.top + 18)
    else right = Math.max(right, map.right - drawer.left + 22)
  }
  top = Math.min(top, Math.max(24, map.height - 200))
  bottom = Math.min(bottom, Math.max(24, map.height - top - 140))
  right = Math.min(right, Math.max(24, map.width - 156))
  return [top, right, bottom, 36]
}

export function itineraryGuideIssue(plan: TripPlanResult): string | null {
  if (plan.preview) return '模拟路线不支持步行引导'
  if (plan.legs.length !== plan.stops.length || !plan.legs.length) return '行程尚不完整'
  if (plan.basis !== 'network') return '未取得完整步行路线'
  const index = plan.legs.findIndex(leg => itineraryLegIssue(leg))
  if (index >= 0) return `第 ${index + 1} 段${itineraryLegIssue(plan.legs[index])}`
  if (!plan.routing_status) return '行程状态数据不完整，请重新规划'
  if (plan.routing_status !== 'clear') return '行程尚未全部通过通行核验'
  if (plan.legs.some((leg, i) => {
    const from = i ? plan.legs[i - 1] : plan.origin
    return leg.category !== plan.stops[i] || !from || Math.abs(leg.from.lat - from.lat) > 1e-6 || Math.abs(leg.from.lng - from.lng) > 1e-6
  })) return '路段起点或入口不连续，请重新规划'
  return null
}

export interface ItineraryAction { kind: 'journey' | 'segment' | 'retry' | 'edit'; label: string; item?: TripItem }
export function itineraryActions(plan: TripPlanResult, selected: string | null, retryBlocked?: string | null): { primary: ItineraryAction; secondary?: ItineraryAction; issue: string | null } {
  const issue = itineraryGuideIssue(plan)
  const known = plan.legs.filter(leg => !itineraryLegIssue(leg, plan.preview) && plan.basis === 'network')
  const segment = known.find(leg => leg.entry_id === selected) ?? known[0]
  const preview: ItineraryAction | undefined = segment ? { kind: 'segment', label: selected === segment.entry_id ? '预览这一段' : `预览第 ${plan.legs.indexOf(segment) + 1} 段`, item: segment } : undefined
  const journey: ItineraryAction = { kind: 'journey', label: '开始步行', item: plan.legs[0] }
  if (!issue) return selected && preview ? { primary: preview, secondary: { ...journey, label: '开始整个行程' }, issue } : { primary: journey, issue }
  const missing = plan.legs.map((leg, i) => itineraryLegIssue(leg, plan.preview) ? i + 1 : 0).filter(Boolean)
  const canRetry = !retryBlocked && plan.basis === 'network' && plan.legs.length === plan.stops.length && plan.legs.length > 0 && plan.routing_status !== 'blocked' && !plan.legs.some(leg => leg.closure_status === 'blocked') && !itineraryRetryIssue(plan)
  if (canRetry) return { primary: { kind: 'retry', label: missing.length ? `重试第 ${missing.join('、')} 段` : '重新核验路线' }, secondary: preview, issue }
  const edit: ItineraryAction = { kind: 'edit', label: '调整行程' }
  return preview ? { primary: preview, secondary: edit, issue } : { primary: edit, issue }
}

export function guideItinerary(items: readonly TripItem[], selected: TripItem, kind: 'journey' | 'segment') {
  const index = kind === 'journey' ? 0 : Math.max(0, items.findIndex(item => item.entry_id === selected.entry_id))
  const item = kind === 'journey' ? items[0] ?? selected : selected
  return {
    item, remaining: kind === 'journey' && items.length ? [...items] : [item],
    context: { kind, index, count: Math.max(1, items.length), fromLabel: index ? `第 ${index} 站 · ${items[index - 1].name}` : '当前起点' },
  }
}

export function tripReplacementIssue(current: TripPlanResult, next: TripPlanResult, index: number, replacement: TripSelection): string | null {
  if (next.legs.length !== current.legs.length || next.stops.join('\0') !== current.stops.join('\0') || next.legs.some((leg, i) => {
    const expected = i === index ? replacement : current.legs[i]
    return leg.category !== current.legs[i]?.category || leg.place_id !== expected.place_id || leg.entry_id !== expected.entry_id
  })) return '新行程的站点或入口不一致，原行程已保留。'
  if (current.basis === 'network' && !current.preview && itineraryGuideIssue(next)) return `${next.warnings.find(w => /预算|配额|失败|围挡/.test(w)) ?? '新行程未通过通行核验'}，原行程已保留。`
  return null
}

export function tripUpdateIssue(previous: TripPlanResult | undefined, next: TripPlanResult): string | null {
  if (!previous || itineraryGuideIssue(previous) || !itineraryGuideIssue(next)) return null
  const failed = next.legs.find(leg => itineraryLegIssue(leg, next.preview))
  return `${failed?.note ?? next.warnings.find(w => /预算|配额|失败|围挡/.test(w)) ?? itineraryGuideIssue(next)}，原行程已保留。`
}

export function tripDraftPins(stops: readonly string[], choices: Readonly<Record<string, TripSelection>>) {
  return stops.flatMap((category, index) => choices[category] ? [{ index, place_id: choices[category].place_id, entry_id: choices[category].entry_id }] : [])
}

export function tripDraftIssue(stops: readonly string[], fixed: readonly TripFixedStop[], next: TripPlanResult): string | null {
  if (stops.join('\0') !== next.stops.join('\0')) return '返回的站序与设置不一致，请重试。'
  for (const pin of fixed) {
    const leg = next.legs[pin.index]
    if (!leg || leg.category !== stops[pin.index] || leg.place_id !== pin.place_id || leg.entry_id !== pin.entry_id) return `第 ${pin.index + 1} 站未保留所选设施和入口，请重新选择。`
  }
  return null
}

export const TRIP_SEGMENT_COLORS = ['#1f7a42', '#2478bc', '#9362bc', '#168b8b', '#b9602a', '#ac4a75'] as const
export function tripSegmentColor(index: number) { return TRIP_SEGMENT_COLORS[Math.max(0, index) % TRIP_SEGMENT_COLORS.length] }

export function itineraryLineStyle(item: TripItem, selected: boolean, hasSelection: boolean, preview = false, index = 0) {
  const measured = !preview && Boolean(item.route && item.route.path.length > 1)
  const clear = measured && item.closure_status === 'clear'
  return {
    color: item.closure_status === 'blocked' ? '#c0392b' : clear ? tripSegmentColor(index) : '#b47d1d',
    weight: selected ? 5.5 : 4,
    alpha: hasSelection && !selected ? .85 : .98,
    dashed: !clear,
    arrows: clear && (!hasSelection || selected),
  }
}

export interface PixelPoint { x: number; y: number }
/** Sample screen distance along the original path; never offset or smooth geometry. */
export function routeArrowPositions(points: readonly PixelPoint[], spacing = 110): (PixelPoint & { angle: number })[] {
  if (!(spacing > 0)) return []
  const result: (PixelPoint & { angle: number })[] = []
  let distance = 0, next = spacing / 2
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i], dx = b.x - a.x, dy = b.y - a.y, length = Math.hypot(dx, dy)
    if (!Number.isFinite(length) || !length) continue
    while (next <= distance + length) {
      if (next >= distance) {
        const t = (next - distance) / length
        result.push({ x: a.x + dx * t, y: a.y + dy * t, angle: Math.atan2(dy, dx) })
      }
      next += spacing
    }
    distance += length
  }
  return result
}

/** Deterministic hit testing also works when a building obscures the SDK's ground polyline. */
export function hitTripRoute(items: readonly TripItem[], selected: string | null, point: PixelPoint, project: (p: readonly [number, number]) => PixelPoint, preview = false): string | null {
  let best = 10, id: string | null = null
  const ordered = [...items].sort((a, b) => Number(b.entry_id === selected) - Number(a.entry_id === selected))
  for (const item of ordered) {
    const paths: [number, number][][] = [itineraryPath(item, preview), item.route?.blocked_path ?? []]
    for (const path of paths) for (let i = 1; i < path.length; i++) {
      const a = project(path[i - 1]), b = project(path[i]), dx = b.x - a.x, dy = b.y - a.y, length2 = dx * dx + dy * dy
      const t = length2 ? Math.max(0, Math.min(1, ((point.x - a.x) * dx + (point.y - a.y) * dy) / length2)) : 0
      const distance = Math.hypot(point.x - a.x - dx * t, point.y - a.y - dy * t)
      if (distance < best - .01) { best = distance; id = item.entry_id }
    }
  }
  return id
}
