import polygonClipping, { type MultiPolygon, type Polygon, type Ring } from 'polygon-clipping'
import type { GridCheck, GridCheckClue, GridCheckGap, GridCheckLeaf, SurfaceGeometry } from '../types'

type Points = readonly (readonly [number, number])[]

const M_PER_DEG_LAT = 111_320

/** 叶子方格的四角，[经度, 纬度]，逆时针。 */
export function leafCorners(leaf: Pick<GridCheckLeaf, 'lat' | 'lng' | 'size_m'>): [number, number][] {
  const dLat = leaf.size_m / 2 / M_PER_DEG_LAT
  const dLng = leaf.size_m / 2 / (M_PER_DEG_LAT * Math.cos((leaf.lat * Math.PI) / 180))
  return [
    [leaf.lng - dLng, leaf.lat - dLat],
    [leaf.lng + dLng, leaf.lat - dLat],
    [leaf.lng + dLng, leaf.lat + dLat],
    [leaf.lng - dLng, leaf.lat + dLat],
  ]
}

/**
 * 一片缺口的显示几何：各叶子方格合并，再裁到射线圈里（缺口说的是「射线圈包进来、实际走不到」的那部分）。
 * 返回若干多边形（外环 + 孔洞）。只裁剪显示几何，不改动结果里的数据。
 */
export function gapPolygons(gap: Pick<GridCheckGap, 'leaves'>, circle: Points, fused?: SurfaceGeometry): Polygon[] {
  if (circle.length < 3 || gap.leaves.length === 0) return []
  const squares: Polygon[] = gap.leaves.map(leaf => [leafCorners(leaf) as Ring])
  const merged: MultiPolygon = polygonClipping.union(squares[0], ...squares.slice(1))
  const clipped = polygonClipping.intersection(merged, [circle.map(([x, y]) => [x, y]) as Ring])
  return fused ? polygonClipping.difference(clipped, fused.type === 'Polygon' ? [fused.coordinates] : fused.coordinates) : clipped
}

/** 秒 → 「约 21 分钟」。 */
export function minutesText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  return `约 ${Math.round(seconds / 60)} 分钟`
}

export const DELAY_SOURCE_TEXT: Record<GridCheckLeaf['delay_source'], string> = {
  ray_interp: '过街等待取自相邻射线（插值）',
  own_route: '过街等待取自这个点自己的步行路线',
  none: '未计过街等待',
}

/** 一片缺口为什么算「确认」：不计等待就已超时，或用自己的路线补上等待后超时。 */
export function gapReason(leaf: GridCheckLeaf, targetS: number): string {
  if (leaf.blocked) return '从中心过来的路线穿过施工围挡'
  if (leaf.raw_s !== null && leaf.raw_s > targetS) return `不计过街等待就已超过 ${targetS / 60} 分钟`
  return `用这个点自己的步行路线补上过街等待后超过 ${targetS / 60} 分钟`
}

/** 这片缺口有没有用路线核验过：路线规划与批量算路是两个接口，结论相反的点已记为存疑、不在片里。 */
export function routeCheckText(gap: Pick<GridCheckGap, 'route_confirmed'>, targetS = 900): string {
  if (gap.route_confirmed === undefined) return ''
  if (gap.route_confirmed > 0) {
    return `这片有 ${gap.route_confirmed} 个点单独取过步行路线，路线自己的耗时同样超过 ${targetS / 60} 分钟。`
  }
  return '这片只有批量算路的结论，路线预算用完、没有取路线核验。'
}

/** 线索附近的缺口 → 线索对象。 */
export function cluesOf(check: GridCheck, gap: GridCheckGap): GridCheckClue[] {
  return gap.clues.flatMap(id => check.clues.filter(c => c.id === id))
}

/** 校核估计面积与射线圈面积之差，带正负号的平方公里文本。 */
export function areaDelta(check: Pick<GridCheck, 'estimate_area_km2' | 'ray_area_km2'>): string {
  const d = check.estimate_area_km2 - check.ray_area_km2
  const pct = check.ray_area_km2 > 0 ? (d / check.ray_area_km2) * 100 : 0
  const sign = d > 0 ? '+' : d < 0 ? '−' : '±'
  return `${sign}${Math.abs(d).toFixed(3)} km²（${sign}${Math.abs(pct).toFixed(1)}%）`
}
