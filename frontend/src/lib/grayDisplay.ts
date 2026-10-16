import polygonClipping, { type Ring } from 'polygon-clipping'
import { intersectSurface, surfacePolygons, type SurfaceInput } from './surface.ts'

type Points = readonly (readonly [number, number])[]

const copyRing = (points: Points): Ring => points.map(([lng, lat]) => [lng, lat])

/** 只裁剪显示几何；保留原网格点、缺失判定和报告统计。 */
export function clipBlindCell(corners: Points, circle: Points): Ring[] {
  if (corners.length < 3 || circle.length < 3) return []
  // 生活圈可能是凹多边形，一格相交后也可能分成多个独立片段。
  return polygonClipping.intersection([copyRing(corners)], [copyRing(circle)])
    .map(polygon => polygon[0])
}

/** 区域轮廓包含外环、孔洞及独立片段；按奇偶规则组合后再裁剪。 */
export function clipRegionOutlines(rings: readonly Points[], circle: SurfaceInput): Ring[] {
  const polygons = rings.filter(ring => ring.length >= 3).map(ring => [copyRing(ring)])
  const boundary = surfacePolygons(circle)
  if (!polygons.length || !boundary.length) return []
  const region = polygonClipping.xor(polygons[0], ...polygons.slice(1))
  return polygonClipping.intersection(region, boundary).flatMap(polygon => polygon)
}

export function clipBlindSurface(corners: Points, circle: SurfaceInput) {
  return intersectSurface(corners, circle)
}
