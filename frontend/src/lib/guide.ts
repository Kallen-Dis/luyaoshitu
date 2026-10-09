import type { TripItem, TripGuideData, TripReanchorResult } from '../types'

export const MAX_LOCATION_ACCURACY_M = 100

export function locationQuality(point: TripGuideData['location'], now: number): string | null {
  if (!point) return '请先定位到当前位置。'
  if (!Number.isFinite(point.timestamp) || now - point.timestamp > 30000 || point.timestamp > now + 1000) return '定位已过期，请重新定位后出发。'
  if (!Number.isFinite(point.accuracy) || point.accuracy < 0 || point.accuracy > MAX_LOCATION_ACCURACY_M) return `定位精度超过 ${MAX_LOCATION_ACCURACY_M} 米，请重新定位或退出引导后在地图上设置起点。`
  return null
}

export function departureDistance(point: NonNullable<TripGuideData['location']>, origin: { lat: number; lng: number }): { meters: number; kind: 'near' | 'far' | 'uncertain' } {
  const rad = Math.PI / 180
  const h = Math.sin((point.lat - origin.lat) * rad / 2) ** 2 + Math.cos(point.lat * rad) * Math.cos(origin.lat * rad) * Math.sin((point.lng - origin.lng) * rad / 2) ** 2
  const meters = 6371000 * 2 * Math.asin(Math.sqrt(Math.min(1, h)))
  const error = point.accuracy + 10
  return { meters, kind: meters + error <= 50 ? 'near' : meters - error > 50 ? 'far' : 'uncertain' }
}

export function acceptReanchor(current: TripGuideData, result: TripReanchorResult, expected: readonly TripItem[]): TripGuideData {
  if (result.preview || result.basis !== 'network' || result.routing_status !== 'clear' || result.legs.length !== expected.length || result.legs.some((leg, i) => guideUnavailableReason(leg) || leg.place_id !== expected[i].place_id || leg.entry_id !== expected[i].entry_id)) {
    const reason = result.legs.find(leg => leg.closure_status !== 'clear')?.note
    throw new Error(reason ?? result.warnings.find(w => /预算|配额|失败|核验/.test(w)) ?? '新行程未通过通行核验，原计划保留。')
  }
  return { ...current, item: result.legs[0], remaining: result.legs, routingFeature: result.routing_feature, stepIndex: 0, mode: 'walking', focus: 'origin', focusKey: current.focusKey + 1 }
}

export interface Deviation { key: string; since: number; stamp: number; count: number; confirmed: boolean }
export function updateDeviation(previous: Deviation | null, point: TripGuideData['location'], path: readonly (readonly [number, number])[], key: string, now: number): Deviation | null {
  if (locationQuality(point, now) || !point || distanceToRoute(point, path) <= Math.max(60, point.accuracy * 1.5)) return null
  const old = previous?.key === key ? previous : null
  const since = old?.since ?? now
  const count = old ? old.count + Number(old.stamp !== point.timestamp) : 1
  return { key, since, stamp: point.timestamp, count, confirmed: count >= 2 && now - since >= 5000 }
}

export function originViewBounds(item: TripItem): [number, number][] {
  const origin = item.from, sx = 111320 * Math.cos(origin.lat * Math.PI / 180)
  const bounds: [number, number][] = [[origin.lng, origin.lat]]
  const path = item.route?.steps[0]?.path ?? item.route?.path ?? []
  let walked = 0
  for (let i = 0; i < path.length; i++) {
    if (i) {
      const distance = Math.hypot((path[i][0] - path[i - 1][0]) * sx, (path[i][1] - path[i - 1][1]) * 111320)
      if (walked + distance > 120) {
        const t = distance ? (120 - walked) / distance : 0
        bounds.push([path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t, path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t])
        break
      }
      walked += distance
    }
    bounds.push([path[i][0], path[i][1]])
  }
  // 很短的第一步也保留可读的街道范围，避免缩放到极限。
  for (const x of [-25, 25]) for (const y of [-25, 25]) bounds.push([origin.lng + x / sx, origin.lat + y / 111320])
  return bounds
}

export function afterStableMapSize(read: () => { w: number; h: number }, apply: () => void, frame = requestAnimationFrame, cancel = cancelAnimationFrame): () => void {
  let active = true, id = 0, previous = { w: 0, h: 0 }, stable = 0
  const tick = () => {
    if (!active) return
    const size = read()
    stable = size.w > 0 && size.h > 0 && size.w === previous.w && size.h === previous.h ? stable + 1 : 0
    previous = size
    if (stable >= 2) apply()
    else id = frame(tick)
  }
  id = frame(tick)
  return () => { active = false; cancel(id) }
}

/** 停止监听后丢弃已排队的回调，避免退出引导后重新写入引导状态。 */
export function observeGps(geolocation: Pick<Geolocation, 'watchPosition' | 'clearWatch'>, onPosition: PositionCallback, onError: PositionErrorCallback): () => void {
  let active = true
  const id = geolocation.watchPosition(position => { if (active) onPosition(position) }, error => { if (active) onError(error) }, { enableHighAccuracy: true, maximumAge: 0, timeout: 15000 })
  return () => { active = false; geolocation.clearWatch(id) }
}

export function guideUnavailableReason(item: TripItem, preview = false): string | null {
  if (preview) return '模拟路线不支持引导'
  if (item.closure_status === 'blocked') return '路线受阻，请另选一家'
  if (!item.route || item.route.path.length < 2) return '未取得步行路线'
  if (item.closure_status !== 'clear') return '通行情况待核验'
  if (!item.route.steps.length) return '尚未取得步行步骤'
  return null
}

/** WGS84 经 GCJ02 近似换算为 BD09；仅在内存中进行，不承诺测绘精度。 */
export function gpsToBaidu(lat: number, lng: number): { lat: number; lng: number } | null {
  if (!Number.isFinite(lat) || !Number.isFinite(lng) || lat < 3.8 || lat > 53.6 || lng < 73.4 || lng > 135.1) return null
  const x = lng - 105, y = lat - 35, pi = Math.PI
  let dlat = -100 + 2 * x + 3 * y + .2 * y * y + .1 * x * y + .2 * Math.sqrt(Math.abs(x))
  dlat += (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3
  dlat += (20 * Math.sin(y * pi) + 40 * Math.sin(y / 3 * pi)) * 2 / 3
  dlat += (160 * Math.sin(y / 12 * pi) + 320 * Math.sin(y * pi / 30)) * 2 / 3
  let dlng = 300 + x + 2 * y + .1 * x * x + .1 * x * y + .1 * Math.sqrt(Math.abs(x))
  dlng += (20 * Math.sin(6 * x * pi) + 20 * Math.sin(2 * x * pi)) * 2 / 3
  dlng += (20 * Math.sin(x * pi) + 40 * Math.sin(x / 3 * pi)) * 2 / 3
  dlng += (150 * Math.sin(x / 12 * pi) + 300 * Math.sin(x / 30 * pi)) * 2 / 3
  const eccentricity = .006693421622965943
  const rad = lat / 180 * pi, magic = 1 - eccentricity * Math.sin(rad) ** 2
  const sqrt = Math.sqrt(magic), a = 6378245
  const gcjLat = lat + dlat * 180 / ((a * (1 - eccentricity) / (magic * sqrt)) * pi)
  const gcjLng = lng + dlng * 180 / (a / sqrt * Math.cos(rad) * pi)
  const xp = pi * 3000 / 180
  const z = Math.hypot(gcjLng, gcjLat) + .00002 * Math.sin(gcjLat * xp)
  const theta = Math.atan2(gcjLat, gcjLng) + .000003 * Math.cos(gcjLng * xp)
  return { lat: z * Math.sin(theta) + .006, lng: z * Math.cos(theta) + .0065 }
}

/** 点到路线各线段的最近距离；仅供偏离提示，不自动换路。 */
export function distanceToRoute(point: { lat: number; lng: number }, path: readonly (readonly [number, number])[]): number {
  if (path.length < 2) return Infinity
  const sx = 111320 * Math.cos(point.lat * Math.PI / 180), sy = 111320
  let best = Infinity
  for (let i = 1; i < path.length; i++) {
    const ax = (path[i - 1][0] - point.lng) * sx, ay = (path[i - 1][1] - point.lat) * sy
    const bx = (path[i][0] - point.lng) * sx, by = (path[i][1] - point.lat) * sy
    const dx = bx - ax, dy = by - ay, length2 = dx * dx + dy * dy
    const t = length2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / length2)) : 0
    best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy))
  }
  return best
}
