import earcut, { deviation, flatten } from 'earcut'
import polygonClipping, { type MultiPolygon, type Polygon, type Ring } from 'polygon-clipping'
import type { SurfaceGeometry } from '../types'

export type Points = readonly (readonly [number, number])[]
export type SurfaceInput = SurfaceGeometry | Points

export function surfacePolygons(shape: SurfaceInput | null | undefined): MultiPolygon {
  if (!shape) return []
  if (Array.isArray(shape)) return shape.length >= 3 ? [[shape.map(([x, y]) => [x, y]) as Ring]] : []
  const geometry = shape as SurfaceGeometry
  const faces = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates
  return faces.filter(face => face[0]?.length >= 3).map(face => face.map(ring => ring.map(([x, y]) => [x, y])))
}

export function surfaceRings(shape: SurfaceInput | null | undefined): Ring[] {
  return surfacePolygons(shape).flat()
}

export function outerPoints(shape: SurfaceInput | null | undefined): [number, number][] {
  return surfacePolygons(shape).flatMap(face => face[0])
}

/** 仅兼容旧接口需要单个外环的场合；主显示与圈内判断必须使用完整几何。 */
export function firstOuterRing(shape: SurfaceInput | null | undefined): [number, number][] {
  return surfacePolygons(shape)[0]?.[0] ?? []
}

export function pointInSurface(lng: number, lat: number, shape: SurfaceInput): boolean {
  const inRing = (ring: Ring) => {
    let inside = false
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [ax, ay] = ring[i]
      const [bx, by] = ring[j]
      if ((ay > lat) !== (by > lat) && lng < (bx - ax) * (lat - ay) / (by - ay) + ax) inside = !inside
    }
    return inside
  }
  return surfacePolygons(shape).some(face => inRing(face[0]) && !face.slice(1).some(inRing))
}

export function intersectSurface(a: SurfaceInput, b: SurfaceInput): MultiPolygon {
  const left = surfacePolygons(a)
  const right = surfacePolygons(b)
  return left.length && right.length ? polygonClipping.intersection(left, right) : []
}

export function addedSurface(current: SurfaceGeometry, baseline: SurfaceGeometry): MultiPolygon {
  const a = surfacePolygons(current)
  const b = surfacePolygons(baseline)
  return a.length ? b.length ? polygonClipping.difference(a, b) : a : []
}

/** 百度地面 Polygon 不表达孔洞；只在有孔洞时切成三角片，绝不把洞填回去。 */
export function fillPieces(face: Polygon): Ring[] {
  const rings = face.map(ring => {
    const points = ring.map(([x, y]) => [x, y] as [number, number])
    if (points.length > 1 && points[0][0] === points.at(-1)![0] && points[0][1] === points.at(-1)![1]) points.pop()
    return points
  }).filter(ring => ring.length >= 3)
  if (!rings.length) return []
  if (rings.length === 1) return [rings[0]]
  const data = flatten(rings)
  const indices = earcut(data.vertices, data.holes, data.dimensions)
  if (deviation(data.vertices, data.holes, data.dimensions, indices) > 0.00001) return []
  const pieces: Ring[] = []
  for (let i = 0; i < indices.length; i += 3) {
    pieces.push(indices.slice(i, i + 3).map(index => [data.vertices[index * 2], data.vertices[index * 2 + 1]] as [number, number]))
  }
  return pieces
}
