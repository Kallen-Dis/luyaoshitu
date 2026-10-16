import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { addedSurface, fillPieces, intersectSurface, pointInSurface, surfacePolygons, surfaceRings } from '../src/lib/surface.ts'

const square = (x, y, size) => [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]
const surface = { type: 'MultiPolygon', coordinates: [[square(0, 0, 10), square(2, 2, 3)], [square(20, 0, 5)]] }
const area = ring => Math.abs(ring.reduce((s, p, i) => {
  const q = ring[(i + 1) % ring.length]
  return s + p[0] * q[1] - q[0] * p[1]
}, 0)) / 2
const multiArea = faces => faces.reduce((s, face) => s + area(face[0]) - face.slice(1).reduce((n, hole) => n + area(hole), 0), 0)

test('real sample surfaces triangulate every component without losing its area', () => {
  const localArea = ring => {
    const [x, y] = ring[0]
    return area(ring.map(p => [p[0] - x, p[1] - y]))
  }
  for (const name of ['caoyang', 'taopu']) {
    const feature = JSON.parse(readFileSync(new URL(`../../data/samples/isochrone-${name}-15min.geojson`, import.meta.url), 'utf8'))
    for (const face of surfacePolygons(feature.geometry)) {
      const expected = localArea(face[0]) - face.slice(1).reduce((s, r) => s + localArea(r), 0)
      const triangles = fillPieces(face)
      assert.ok(triangles.length, `${name}: each component is visible`)
      const actual = triangles.reduce((s, r) => s + localArea(r), 0)
      assert.ok(Math.abs(expected - actual) < Math.max(expected * 1e-5, 1e-14), `${name}: holes remain unfilled`)
    }
  }
})

test('real inner contours stay inside the main circle and do not overlap pending regions', () => {
  let fused = 0
  for (const name of ['caoyang', 'taopu']) {
    const feature = JSON.parse(readFileSync(new URL(`../../data/samples/isochrone-${name}-15min.geojson`, import.meta.url), 'utf8'))
    if (!feature.properties.fusion) {
      assert.equal(feature.properties.geometry_method, 'ray')
      assert.ok(feature.properties.fusion_fallback)
      continue
    }
    fused++
    const { lat } = feature.properties.center
    const metreArea = faces => multiArea(faces.map(face => face.map(ring => {
      const [x, y] = ring[0]
      return ring.map(p => [(p[0] - x) * 111320 * Math.cos(lat * Math.PI / 180), (p[1] - y) * 111320])
    })))
    for (const inner of feature.properties.rings) {
      // 坐标保留 8 位小数；公共边舍入可能留下远小于一平方米的狭缝。
      assert.ok(metreArea(addedSurface(inner.geometry, feature.geometry)) < 1, `${name}: ${inner.minutes} min must be inside the main estimate`)
      assert.ok(metreArea(intersectSurface(inner.geometry, feature.properties.fusion.unknown_geometry)) < 1, `${name}: inner contour must exclude pending holes`)
    }
  }
  assert.ok(fused > 0, 'at least one accepted field must be checked')
})

test('all components and holes participate in membership and clipping', () => {
  assert.equal(pointInSurface(1, 1, surface), true)
  assert.equal(pointInSurface(3, 3, surface), false)
  assert.equal(pointInSurface(22, 2, surface), true)
  assert.equal(pointInSurface(15, 2, surface), false)
  assert.equal(surfaceRings(surface).length, 3)
  assert.equal(multiArea(intersectSurface(surface, square(-1, -1, 30))), 116)
})

test('native map fill triangles do not paint the hole green in 2D or 3D', () => {
  const pieces = fillPieces(surfacePolygons(surface)[0])
  assert.ok(pieces.length > 1)
  assert.equal(pieces.reduce((s, ring) => s + area(ring), 0), 91)
  for (const triangle of pieces) {
    const x = triangle.reduce((s, p) => s + p[0], 0) / triangle.length
    const y = triangle.reduce((s, p) => s + p[1], 0) / triangle.length
    assert.equal(pointInSurface(x, y, surface), true)
  }
})

test('added range is the true geometry difference and keeps holes', () => {
  const baseline = { type: 'Polygon', coordinates: [square(0, 0, 10)] }
  const added = addedSurface(surface, baseline)
  assert.equal(multiArea(added), 25)
  assert.deepEqual(surface.coordinates[0][1], square(2, 2, 3))
})
