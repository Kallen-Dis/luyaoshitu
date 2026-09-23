import type { Place } from '../types'

/** 与圈见相同的铺格：中心周围 1.5 公里、200 米一格，圈外也画，所以遮盖比等时圈更大。 */
const CELL_M = 200
const EXTENT_M = 1500
const RADIUS_M = 1000
const KEYS = ['生鲜采买', '医药', '基础教育'] as const

export interface WideCell {
  lat: number
  lng: number
  missing: string[]
  inCircle: boolean
}

function haversineM(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const r = 6_371_000
  const p1 = (aLat * Math.PI) / 180
  const p2 = (bLat * Math.PI) / 180
  const dp = ((bLat - aLat) * Math.PI) / 180
  const dl = ((bLng - aLng) * Math.PI) / 180
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)))
}

function inRing(lat: number, lng: number, ring: [number, number][]): boolean {
  let inside = false
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = [ring[i][0], ring[i][1]]
    const [xj, yj] = [ring[j][0], ring[j][1]]
    const hit = yi > lat !== yj > lat && lng < ((xj - xi) * (lat - yi)) / (yj - yi + 0.0) + xi
    if (hit) inside = !inside
  }
  return inside
}

function offset(lat: number, lng: number, eastM: number, northM: number) {
  const dLat = northM / 111_320
  const dLng = eastM / (111_320 * Math.cos((lat * Math.PI) / 180))
  return { lat: lat + dLat, lng: lng + dLng }
}

export interface WideCensus {
  total: number
  blind: number
  missing: Record<string, number>
}

/** 与地图红格同一套计数：总共铺了多少格、其中多少格缺硬指标。 */
export function wideCensus(
  center: { lat: number; lng: number },
  places: Place[],
  ring: [number, number][],
): WideCensus {
  const all = scanCells(center, places, ring)
  const missing: Record<string, number> = {}
  for (const key of KEYS) {
    missing[key] = all.filter((cell) => cell.missing.includes(key)).length
  }
  const blind = all.filter((cell) => cell.missing.length > 0).length
  return { total: all.length, blind, missing }
}

/** 直线 1 公里内缺硬指标的格子。圈外的格子用更淡的颜色画。 */
export function wideBlindCells(
  center: { lat: number; lng: number },
  places: Place[],
  ring: [number, number][],
): WideCell[] {
  return scanCells(center, places, ring).filter((cell) => cell.missing.length > 0)
}

function scanCells(
  center: { lat: number; lng: number },
  places: Place[],
  ring: [number, number][],
): WideCell[] {
  const buckets = new Map<string, Place[]>()
  for (const key of KEYS) buckets.set(key, [])
  for (const place of places) buckets.get(place.category)?.push(place)

  const n = Math.floor(EXTENT_M / CELL_M)
  const cells: WideCell[] = []
  for (let iy = -n; iy <= n; iy++) {
    for (let ix = -n; ix <= n; ix++) {
      if (Math.hypot(ix * CELL_M, iy * CELL_M) > EXTENT_M) continue
      const p = offset(center.lat, center.lng, ix * CELL_M, iy * CELL_M)
      const missing = KEYS.filter((key) => {
        const list = buckets.get(key) ?? []
        return !list.some((f) => haversineM(p.lat, p.lng, f.lat, f.lng) <= RADIUS_M)
      })
      cells.push({
        ...p,
        missing: [...missing],
        inCircle: inRing(p.lat, p.lng, ring),
      })
    }
  }
  return cells
}

export const WIDE_CELL_M = CELL_M
