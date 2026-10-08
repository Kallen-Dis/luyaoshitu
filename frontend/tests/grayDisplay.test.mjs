import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { clipBlindCell, clipRegionOutlines } from '../src/lib/grayDisplay.ts'

const square = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
const area = ring => Math.abs(ring.reduce((sum, point, index) => {
  const next = ring[(index + 1) % ring.length]
  return sum + point[0] * next[1] - next[0] * point[1]
}, 0)) / 2

test('a boundary cell is cut at the circle edge without modifying its inputs', () => {
  const cell = square(0, 0, 2, 2)
  const circle = [[-1, -1], [3, -1], [-1, 3]]
  const source = JSON.stringify({ cell, circle })
  const parts = clipBlindCell(cell, circle)
  assert.equal(parts.length, 1)
  assert.equal(area(parts[0]), 2)
  assert.ok(parts.flat().every(([x, y]) => x + y <= 2))
  assert.equal(JSON.stringify({ cell, circle }), source)
})

test('clipping a concave circle keeps disconnected pieces separate', () => {
  const circle = [[0, 0], [3, 0], [3, 3], [2, 3], [2, 1], [1, 1], [1, 3], [0, 3]]
  const parts = clipBlindCell(square(-1, 2, 4, 4), circle)
  assert.equal(parts.length, 2)
  assert.equal(parts.reduce((sum, ring) => sum + area(ring), 0), 2)
})

test('region clipping preserves holes and removes an opened hole from the outer outline', () => {
  const region = [square(0, 0, 4, 4), square(1, 1, 3, 3)]
  const closed = clipRegionOutlines(region, square(-1, -1, 5, 5))
  assert.deepEqual(closed.map(area).sort((a, b) => a - b), [4, 16])
  const cut = clipRegionOutlines(region, square(2, -1, 5, 5))
  assert.equal(cut.length, 1)
  assert.equal(area(cut[0]), 6)
  assert.ok(cut[0].every(([x]) => x >= 2))
})

test('outside and edge-touching geometry produce no gray fill or outline', () => {
  const circle = square(0, 0, 1, 1)
  assert.deepEqual(clipBlindCell(square(1, 0, 2, 1), circle), [])
  assert.deepEqual(clipRegionOutlines([square(2, 2, 3, 3)], circle), [])
})

test('the real sample C outline is clipped while the snapshot and report remain intact', () => {
  const feature = JSON.parse(readFileSync(new URL('../../data/samples/isochrone-caoyang-15min.geojson', import.meta.url), 'utf8'))
  const source = JSON.stringify(feature)
  // 快照内区域由原网格生成；这里只用现版圈内C区对应的单格外环作显示裁剪。
  const circle = feature.geometry.coordinates[0]
  const cell = { lat: 31.244053, lng: 121.419224 }
  const halfLat = 50 / 111320
  const halfLng = halfLat / Math.cos(cell.lat * Math.PI / 180)
  const corners = square(cell.lng - halfLng, cell.lat - halfLat, cell.lng + halfLng, cell.lat + halfLat)
  const parts = clipBlindCell(corners, circle)
  assert.ok(parts.length > 0)
  assert.ok(parts.reduce((sum, ring) => sum + area(ring), 0) < area(corners))
  assert.equal(JSON.stringify(feature), source)
})
