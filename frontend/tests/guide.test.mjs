import assert from 'node:assert/strict'
import test from 'node:test'
import { gpsToBaidu, distanceToRoute, guideUnavailableReason, observeGps, departureDistance, locationQuality, acceptReanchor, updateDeviation, originViewBounds, afterStableMapSize } from '../src/lib/guide.ts'

test('departure classification accounts for accuracy around the 50 meter threshold', () => {
  const origin = { lat: 31.25, lng: 121.42 }
  const fix = { ...origin, accuracy: 5, timestamp: 10000 }
  assert.equal(departureDistance({ ...fix, lat: origin.lat + 25 / 111195 }, origin).kind, 'near')
  assert.equal(departureDistance({ ...fix, lat: origin.lat + 90 / 111195 }, origin).kind, 'far')
  assert.equal(departureDistance({ ...fix, lat: origin.lat + 45 / 111195 }, origin).kind, 'uncertain')
  assert.equal(departureDistance({ ...fix, accuracy: 100 }, origin).kind, 'uncertain')
})

test('old or coarse locations cannot start a new network plan', () => {
  const fix = { lat: 31.25, lng: 121.42, accuracy: 5, timestamp: 10000 }
  assert.equal(locationQuality(fix, 10010), null)
  assert.ok(locationQuality(null, 10000))
  assert.equal(locationQuality({ ...fix, accuracy: 82 }, 10010), null)
  assert.equal(locationQuality({ ...fix, accuracy: 100 }, 10010), null)
  assert.ok(locationQuality({ ...fix, accuracy: 100.1 }, 10010))
  assert.ok(locationQuality(fix, 40001))
  assert.ok(locationQuality({ ...fix, timestamp: 100000 }, 10000))
})

test('replanning commits all matching destinations together or keeps the old plan untouched', () => {
  const route = { path: [[121.42, 31.25], [121.422, 31.25]], steps: [{ instruction: '向前步行' }] }
  const leg = { place_id: 'store', entry_id: 'north', closure_status: 'clear', route }
  const second = { ...leg, place_id: 'school', entry_id: 'school-east' }
  const current = { item: leg, remaining: [leg, second], focusKey: 3, stepIndex: 2, mode: 'preview' }
  const original = JSON.stringify(current)
  const result = { basis: 'network', routing_status: 'clear', legs: [leg, second], warnings: [], routing_feature: {} }
  const next = acceptReanchor(current, result, current.remaining)
  assert.equal(next.mode, 'walking')
  assert.equal(next.stepIndex, 0)
  assert.equal(next.focusKey, 4)
  for (const bad of [
    { ...result, preview: true },
    { ...result, legs: [leg] },
    { ...result, legs: [leg, { ...second, entry_id: 'school-west' }] },
    { ...result, routing_status: 'blocked', legs: [{ ...leg, closure_status: 'blocked' }, second] },
    { ...result, basis: 'estimate_quota', warnings: ['预算不足'] },
  ]) assert.throws(() => acceptReanchor(current, bad, current.remaining))
  assert.equal(JSON.stringify(current), original)
})

test('deviation requires sustained distinct fixes, not movement away from the old origin', () => {
  const route = [[121.42, 31.25], [121.424, 31.25]], start = 10000
  const fix = { lat: 31.251, lng: 121.422, accuracy: 5, timestamp: start }
  let state = updateDeviation(null, fix, route, 'route-a', start)
  assert.equal(state.confirmed, false)
  state = updateDeviation(state, fix, route, 'route-a', start + 6000)
  assert.equal(state.confirmed, false)
  state = updateDeviation(state, { ...fix, timestamp: start + 6000 }, route, 'route-a', start + 6000)
  assert.equal(state.confirmed, true)
  assert.equal(updateDeviation(state, { ...fix, lat: 31.25, timestamp: 17000 }, route, 'route-a', 17000), null)
  assert.equal(updateDeviation(state, { ...fix, accuracy: 500 }, route, 'route-a', 17000), null)
  assert.equal(updateDeviation(state, { ...fix, timestamp: 17000 }, route, 'route-b', 17000).confirmed, false)
})

test('initial view keeps a readable neighborhood and does not fit the distant destination', () => {
  const item = { from: { lat: 31.25, lng: 121.42 }, route: { steps: [{ path: [[121.42, 31.25], [121.42, 31.27]] }] } }
  const original = JSON.stringify(item)
  const bounds = originViewBounds(item)
  assert.ok(bounds.every(([lng, lat]) => lng > 121.419 && lng < 121.421 && lat < 31.252))
  assert.ok(bounds.some(([, lat]) => lat < 31.25))
  assert.equal(JSON.stringify(item), original)
})

test('camera waits for stable layout and canceled work does not claim it has focused', () => {
  let size = { w: 640, h: 720 }, sequence = 0, applied = 0
  const jobs = new Map()
  const frame = callback => { jobs.set(++sequence, callback); return sequence }
  const cancel = id => jobs.delete(id)
  const tick = () => { const [id, callback] = jobs.entries().next().value; jobs.delete(id); callback(0) }
  const stop = afterStableMapSize(() => size, () => applied++, frame, cancel)
  tick()
  const lateCallback = jobs.values().next().value
  stop(); lateCallback(0)
  assert.equal(applied, 0)
  afterStableMapSize(() => size, () => applied++, frame, cancel)
  tick(); size = { w: 1280, h: 720 }; tick(); tick()
  assert.equal(applied, 0)
  tick()
  assert.equal(applied, 1)
})

test('stopping GPS suppresses queued fixes and errors after leaving the guide', () => {
  let success, failure, cleared
  const received = []
  const stop = observeGps({
    watchPosition: (a, b) => { success = a; failure = b; return 17 },
    clearWatch: id => { cleared = id },
  }, point => received.push(point), error => received.push(error))
  success({ coords: { latitude: 31.25, longitude: 121.42 } })
  assert.equal(received.length, 1)
  stop()
  assert.equal(cleared, 17)
  success({ coords: { latitude: 31.251, longitude: 121.42 } })
  failure({ code: 1 })
  assert.equal(received.length, 1)
})

test('only checked real routes with geometry and steps enter walking guidance', () => {
  const item = { closure_status: 'clear', route: { path: [[121.42, 31.25], [121.422, 31.25]], steps: [{ instruction: '向前步行' }] } }
  assert.equal(guideUnavailableReason(item), null)
  assert.equal(guideUnavailableReason(item, true), '模拟路线不支持引导')
  assert.equal(guideUnavailableReason({ ...item, closure_status: 'blocked' }), '路线受阻，请另选一家')
  assert.equal(guideUnavailableReason({ ...item, closure_status: 'unverified' }), '通行情况待核验')
  assert.equal(guideUnavailableReason({ ...item, route: null }), '未取得步行路线')
  assert.equal(guideUnavailableReason({ ...item, route: { ...item.route, path: [] } }), '未取得步行路线')
  assert.equal(guideUnavailableReason({ ...item, route: { ...item.route, steps: [] } }), '尚未取得步行步骤')
})

test('Shanghai GPS transforms into the expected Baidu neighborhood', () => {
  const point = gpsToBaidu(31.25, 121.42)
  assert.ok(point.lat > 31.253 && point.lat < 31.255)
  assert.ok(point.lng > 121.430 && point.lng < 121.432)
})
test('invalid or unsupported GPS does not place a misleading location marker', () => {
  for (const point of [[NaN, 121.42], [31.25, Infinity], [0, 0], [121.42, 31.25], [60, 100]]) assert.equal(gpsToBaidu(...point), null)
})
test('deviation checks the segment interior and endpoints, not just vertices', () => {
  const route = [[121.42, 31.25], [121.422, 31.25]]
  assert.equal(distanceToRoute({ lat: 31.25, lng: 121.421 }, route), 0)
  assert.ok(Math.abs(distanceToRoute({ lat: 31.251, lng: 121.421 }, route) - 111.32) < .01)
  assert.ok(distanceToRoute({ lat: 31.25, lng: 121.423 }, route) > 90)
  assert.ok(Number.isFinite(distanceToRoute({ lat: 31.25, lng: 121.42 }, [route[0], route[0]])))
  assert.equal(distanceToRoute({ lat: 31.25, lng: 121.42 }, []), Infinity)
})
