import assert from 'node:assert/strict'
import test from 'node:test'
import { overviewViewportKey } from '../src/lib/mapViewport.ts'

test('metadata and report updates keep the camera; a different boundary refits', () => {
  const feature = { geometry: { type: 'Polygon', coordinates: [[[1, 1], [2, 1], [1, 2], [1, 1]]] }, properties: { center: {lat: 1, lng: 1} } }
  assert.equal(overviewViewportKey(feature), overviewViewportKey({...feature, properties: {...feature.properties, report: {total: 80}}}))
  const next = structuredClone(feature)
  next.geometry.coordinates[0][1] = [3, 1]
  assert.notEqual(overviewViewportKey(feature), overviewViewportKey(next))
})
