import assert from 'node:assert/strict'
import test from 'node:test'
import { algorithmView, viewSwitch } from '../src/lib/resultView.ts'

test('switching source restores geometry, report, facilities and grids as a single result', () => {
  const geometry = { type: 'MultiPolygon', coordinates: [[[[1, 1], [2, 1], [1, 2], [1, 1]]], [[[3, 3], [4, 3], [3, 4], [3, 3]]]] }
  const baseline = { geometry, cells_changed: [], properties: { report: { total: 70 }, coverage: { categories: { 医药: 2 } }, blindspots: { cells: [{ lat: 1, lng: 1 }] }, geometry_method: 'fusion', fusion: { area_km2: 1 } } }
  const feature = { geometry: { type: 'Polygon', coordinates: [] }, properties: { report: { total: 90 }, markings: { mode: 'auto', baseline } } }
  const before = structuredClone(feature)
  assert.equal(viewSwitch(feature).kind, 'instant')
  const restored = algorithmView(feature)
  assert.deepEqual(restored.geometry, geometry)
  assert.equal(restored.properties.report.total, 70)
  assert.equal(restored.properties.coverage.categories.医药, 2)
  assert.equal(restored.properties.blindspots.cells.length, 1)
  assert.deepEqual(feature, before)
})

test('old partial source diffs require recomputation instead of mixing an old report with a new map', () => {
  const feature = { properties: { markings: { mode: 'auto', baseline: { cells_changed: [] } } } }
  assert.deepEqual(viewSwitch(feature), { kind: 'rerun', to: 'none' })
})
