import assert from 'node:assert/strict'
import test from 'node:test'
import { blindArea, blindCells, cellCount, gridCensus, gridUnits } from '../src/lib/grid.ts'

const parent = (lat, lng, missing, quarters) => ({
  lat, lng, reach_s: 300, nearest_m: {}, missing, unknown: [], in_circle: true,
  ...(quarters ? { quarters } : {}),
})
const quarter = (q, lat, lng, missing, measured = ['基础教育']) => ({ q, lat, lng, nearest_m: {}, missing, unknown: [], measured })

const blindspots = {
  grid_spacing_m: 100, walk_limit_m: 1000, cell_count: 2, blind_count: 1.25, max_reach_s: 300, blind_ratio: {},
  cells: [
    parent(31.0, 121.0, ['基础教育']),
    parent(31.001, 121.0, ['基础教育'], [
      quarter('NW', 31.00122, 120.99974, ['基础教育']),
      quarter('NE', 31.00122, 121.00026, []),
      quarter('SW', 31.00078, 120.99974, []),
      quarter('SE', 31.00078, 121.00026, []),
    ]),
  ],
}

test('split cells are drawn as four 50 m quarters and counted by area', () => {
  const units = gridUnits(blindspots)
  assert.equal(units.length, 5)
  assert.deepEqual(units.map(u => u.size_m), [100, 50, 50, 50, 50])
  assert.equal(units.reduce((sum, u) => sum + u.weight, 0), 2)
  const shown = blindCells(blindspots, '基础教育')
  assert.equal(shown.length, 2)
  assert.equal(blindArea(shown), 1.25)
  const census = gridCensus(blindspots)
  assert.equal(census.total, 2)
  assert.equal(census.blind, 1.25)
  assert.equal(census.refined, true)
  assert.equal(cellCount(census.blind), '1.25')
})

test('quarters keep the parent heat value and say what was measured', () => {
  const [, nw] = gridUnits(blindspots)
  assert.equal(nw.cell.reach_s, 300)
  assert.deepEqual(nw.cell.sub, { q: 'NW', measured: ['基础教育'], size_m: 50 })
  assert.equal('quarters' in nw.cell, false)
})

test('a simulated facility clears a quarter by its own coordinate', () => {
  const simulation = { category: '基础教育', covered_cells: [{ lat: 31.00122, lng: 120.99974 }] }
  assert.equal(blindArea(blindCells(blindspots, '基础教育', simulation)), 1)
})

test('quarters whose centre is outside the ray circle are neither drawn nor counted', () => {
  const edge = {
    ...blindspots,
    cells: [
      parent(31.0, 121.0, ['基础教育']),
      parent(31.001, 121.0, ['基础教育'], [
        quarter('NW', 31.00122, 120.99974, ['基础教育']),
        quarter('NE', 31.00122, 121.00026, []),
        { ...quarter('SW', 31.00078, 120.99974, [], []), in_circle: false },
        { ...quarter('SE', 31.00078, 121.00026, [], []), in_circle: false },
      ]),
    ],
  }
  const units = gridUnits(edge)
  assert.equal(units.length, 3)
  const census = gridCensus(edge)
  assert.equal(census.total, 2) // 父格数不变
  assert.equal(census.units, 1.5) // 面积只算圈内子格
  assert.equal(census.blind, 1.25)
  assert.equal(blindArea(blindCells(edge, '基础教育')), 1.25)
})
