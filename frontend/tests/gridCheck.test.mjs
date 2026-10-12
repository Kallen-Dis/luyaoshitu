import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { areaDelta, gapPolygons, gapReason, leafCorners, minutesText, routeCheckText } from '../src/lib/gridCheck.ts'

const area = ring => Math.abs(ring.reduce((sum, point, index) => {
  const next = ring[(index + 1) % ring.length]
  return sum + point[0] * next[1] - next[0] * point[1]
}, 0)) / 2

test('leaf squares are the right size on the ground', () => {
  const corners = leafCorners({ lat: 31.25, lng: 121.42, size_m: 50 })
  const widthM = (corners[1][0] - corners[0][0]) * 111320 * Math.cos(31.25 * Math.PI / 180)
  const heightM = (corners[2][1] - corners[1][1]) * 111320
  assert.ok(Math.abs(widthM - 50) < 1e-6 && Math.abs(heightM - 50) < 1e-6)
})

test('adjacent leaves of different sizes merge into one piece and are clipped to the ray circle', () => {
  const big = { lat: 31.25, lng: 121.42, size_m: 50 }
  const small = { lat: 31.25, lng: 121.42 + (37.5 / (111320 * Math.cos(31.25 * Math.PI / 180))), size_m: 25 }
  const everything = [[121.0, 31.0], [122.0, 31.0], [122.0, 32.0], [121.0, 32.0]]
  const pieces = gapPolygons({ leaves: [big, small] }, everything)
  assert.equal(pieces.length, 1)
  const half = [[121.0, 31.0], [121.42, 31.0], [121.42, 32.0], [121.0, 32.0]]
  const clipped = gapPolygons({ leaves: [big] }, half)
  assert.ok(Math.abs(area(clipped[0][0]) - area(leafCorners(big)) / 2) < 1e-12)
})

test('a gap reason says which evidence confirmed it', () => {
  const leaf = { raw_s: 1061, eff_s: 1163, blocked: false }
  assert.match(gapReason(leaf, 900), /不计过街等待就已超过/)
  assert.match(gapReason({ ...leaf, raw_s: 860, eff_s: 950 }, 900), /自己的步行路线/)
  assert.equal(minutesText(1261), '约 21 分钟')
  assert.equal(areaDelta({ ray_area_km2: 1.4131, estimate_area_km2: 1.4031 }), '−0.010 km²（−0.7%）')
  assert.equal(areaDelta({ ray_area_km2: 1.18, estimate_area_km2: 1.3 }), '+0.120 km²（+10.2%）')
})

test('every gap stored in the samples lies inside the ray circle and draws as a non-empty piece', () => {
  for (const name of ['caoyang', 'taopu']) {
    const feature = JSON.parse(readFileSync(new URL(`../../data/samples/isochrone-${name}-15min.geojson`, import.meta.url), 'utf8'))
    const check = feature.properties.gridcheck
    assert.ok(check, `${name} has a cross-check`)
    const spacing = feature.properties.blindspots.grid_spacing_m
    for (const gap of check.gaps) {
      const pieces = gapPolygons(gap, feature.geometry.coordinates[0])
      assert.ok(pieces.length > 0, `${name} ${gap.id} draws`)
      for (const leaf of gap.leaves) {
        assert.ok(leaf.blocked || leaf.raw_s > 900 || leaf.delay_source === 'own_route')
        // 方格边长按网格间距算：s、s/2、s/4
        assert.ok([spacing, spacing / 2, spacing / 4].includes(leaf.size_m), `${name} ${gap.id} size ${leaf.size_m}`)
        // 取过路线的点，路线自己的耗时也超过 15 分钟：结论相反的已记为存疑、不在缺口里
        if (leaf.route_s != null && !leaf.blocked) assert.ok(leaf.route_s > 900, `${name} ${gap.id} route ${leaf.route_s}`)
      }
    }
  }
})

test('a gap card says whether a route confirmed it', () => {
  assert.match(routeCheckText({ route_confirmed: 3 }), /3 个点单独取过步行路线/)
  assert.match(routeCheckText({ route_confirmed: 0 }), /只有批量算路的结论/)
  assert.equal(routeCheckText({}), '')
})
