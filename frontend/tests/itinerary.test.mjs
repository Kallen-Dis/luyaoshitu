import assert from 'node:assert/strict'
import test from 'node:test'
import { tripCameraRequest, tripViewBounds, tripViewportMargins, itineraryGuideIssue, guideItinerary, tripReplacementIssue, itineraryLineStyle, routeArrowPositions, hitTripRoute, itineraryActions, itineraryPath, planRequestFor, itineraryRetryIssue, tripUpdateIssue, tripDraftPins, tripDraftIssue, tripSegmentColor } from '../src/lib/itinerary.ts'

const leg = (id, from, end, extra = {}) => ({
  entry_id: id, place_id: id, name: id, category: id === 'school' ? '基础教育' : '生鲜采买', from,
  lng: end[0], lat: end[1], closure_status: 'clear',
  route: { path: [[from.lng, from.lat], end], blocked_path: [], connectors: [], steps: [{ instruction: '前行' }] }, ...extra,
})
const origin = { lng: 121.42, lat: 31.25 }
const items = [leg('store', origin, [121.425, 31.25]), leg('school', { lng: 121.425, lat: 31.25 }, [121.44, 31.26])]
const map = { origin, items, selected: null, hover: null, fitKey: 0, selectionKey: 0, itinerary: true }
const plan = { origin, legs: items, stops: ['生鲜采买', '基础教育'], basis: 'network', routing_status: 'clear', warnings: [], quota: { remaining: { day_pairs: 100, hour_pairs: 100, hour_routes: 10 } } }

test('initial and replaced itineraries fit once; hovering or rerendering does not move the camera', () => {
  assert.deepEqual(tripCameraRequest(null, map), { selected: null })
  assert.equal(tripCameraRequest(map, { ...map, hover: 'school' }), null)
  assert.equal(tripCameraRequest(map, { ...map }), null)
  assert.deepEqual(tripCameraRequest(map, { ...map, items: [...items] }), { selected: null })
  assert.deepEqual(tripCameraRequest(map, { ...map, selected: 'school', selectionKey: 1 }), { selected: 'school' })
  const focused = { ...map, selected: 'school', selectionKey: 1 }
  assert.deepEqual(tripCameraRequest(focused, { ...focused, selectionKey: 2 }), { selected: 'school' })
  assert.deepEqual(tripCameraRequest(focused, { ...focused, selected: null, selectionKey: 2 }), { selected: null })
  assert.equal(tripCameraRequest(null, { ...map, itinerary: false }), null)
  assert.deepEqual(tripCameraRequest(map, { ...map, fitKey: 1 }), { selected: null })
})

test('a selected leg fits its own start, destination and snapped connectors, preserving geometry', () => {
  const original = JSON.stringify(map)
  const selected = tripViewBounds(map, 'school')
  assert.ok(selected.some(([lng]) => lng === items[1].from.lng))
  assert.ok(!selected.some(([lng]) => lng === origin.lng))
  assert.ok(tripViewBounds(map, null).some(([lng]) => lng === origin.lng))
  const snapped = { ...items[1], route: { ...items[1].route, path: [[121.426, 31.25], [121.439, 31.26]], connectors: [[[121.425, 31.25], [121.426, 31.25]]] } }
  const bounds = tripViewBounds({ origin, items: [snapped] }, 'school')
  assert.ok(bounds.some(([lng]) => lng === 121.44))
  assert.ok(bounds.some(([lng]) => lng === 121.425))
  assert.equal(JSON.stringify(map), original)
})

test('viewport avoids the measured desktop drawer and the mobile bottom sheet', () => {
  const desktop = { left: 500, top: 60, right: 1700, bottom: 860, width: 1200, height: 800 }
  const drawer = { left: 1324, top: 136, right: 1684, bottom: 766, width: 360, height: 630 }
  const margins = tripViewportMargins(desktop, drawer)
  assert.equal(margins[1], 398)
  const mobile = { left: 0, top: 0, right: 390, bottom: 800, width: 390, height: 800 }
  const sheet = { left: 8, top: 400, right: 382, bottom: 792, width: 374, height: 392 }
  const mobileMargins = tripViewportMargins(mobile, sheet)
  assert.equal(mobileMargins[2], 418)
  assert.equal(mobileMargins[1], 36)
  const toolbar = { ...drawer, top: 70, bottom: 122 }
  assert.equal(tripViewportMargins(desktop, drawer, toolbar)[0], 80)
  const outside = { ...drawer, left: 2000, right: 2360 }
  assert.deepEqual(tripViewportMargins(desktop, outside), [40, 36, 70, 36])
  const narrow = { left: 420, top: 60, right: 900, bottom: 860, width: 480, height: 800 }
  const narrowSide = { left: 552, top: 136, right: 892, bottom: 844, width: 340, height: 708 }
  const sideMargins = tripViewportMargins(narrow, narrowSide)
  assert.equal(sideMargins[2], 70)
  assert.ok(sideMargins[1] >= 300)
})

test('whole journey always starts at stop one while a segment preview never silently completes prior stops', () => {
  const original = JSON.stringify(items)
  const journey = guideItinerary(items, items[1], 'journey')
  assert.equal(journey.item.entry_id, 'store')
  assert.deepEqual(journey.remaining, items)
  assert.equal(journey.context.index, 0)
  const segment = guideItinerary(items, items[1], 'segment')
  assert.deepEqual(segment.remaining, [items[1]])
  assert.equal(segment.item.from.lng, items[0].lng)
  assert.equal(segment.context.index, 1)
  assert.match(segment.context.fromLabel, /store/)
  assert.equal(JSON.stringify(items), original)
})

test('whole journey cannot start with a blocked, quota degraded or incomplete later leg', () => {
  assert.equal(itineraryGuideIssue(plan), null)
  for (const next of [
    { ...plan, preview: true }, { ...plan, basis: 'estimate_quota' }, { ...plan, legs: [items[0]] },
    { ...plan, legs: [items[0], { ...items[1], closure_status: 'blocked' }] },
    { ...plan, legs: [items[0], { ...items[1], route: { ...items[1].route, steps: [] } }] },
  ]) assert.ok(itineraryGuideIssue(next))
})

test('replacement rejects degraded routes, wrong gates and reordered stops without mutating the usable plan', () => {
  const original = JSON.stringify(plan)
  const choice = { place_id: 'new-store', entry_id: 'new-entry' }
  const first = { ...items[0], ...choice }
  const next = { ...plan, legs: [first, items[1]] }
  assert.equal(tripReplacementIssue(plan, next, 0, choice), null)
  for (const bad of [
    { ...next, basis: 'estimate_quota', warnings: ['预算不足'] },
    { ...next, legs: [first, { ...items[1], entry_id: 'different-gate' }] },
    { ...next, stops: [...plan.stops].reverse() },
    { ...next, legs: [first] },
    { ...next, legs: [first, { ...items[1], closure_status: 'unverified' }] },
  ]) assert.ok(tripReplacementIssue(plan, bad, 0, choice))
  assert.equal(JSON.stringify(plan), original)
})

test('unknown and blocked lines have no direction arrows that imply a verified path', () => {
  assert.equal(itineraryLineStyle(items[0], false, false).arrows, true)
  assert.equal(itineraryLineStyle(items[0], false, true).arrows, false)
  for (const item of [{ ...items[0], route: null }, { ...items[0], closure_status: 'unverified' }, { ...items[0], closure_status: 'blocked' }]) {
    const style = itineraryLineStyle(item, true, true)
    assert.equal(style.arrows, false)
    assert.equal(style.dashed, true)
  }
  assert.equal(itineraryLineStyle(items[0], true, true, true).arrows, false)
})

test('verified segments retain distinct colors and readable weight when another segment is selected', () => {
  const colors = [0, 1, 2, 3, 4, 5].map(tripSegmentColor)
  assert.equal(new Set(colors).size, 6)
  for (let index = 0; index < 6; index++) {
    const normal = itineraryLineStyle(items[0], false, false, false, index)
    const secondary = itineraryLineStyle(items[0], false, true, false, index)
    const active = itineraryLineStyle(items[0], true, true, false, index)
    assert.equal(normal.color, colors[index])
    assert.equal(secondary.color, normal.color)
    assert.equal(active.color, normal.color)
    assert.ok(secondary.alpha >= .8 && secondary.weight >= 4)
    assert.ok(active.weight > secondary.weight)
  }
  assert.notEqual(itineraryLineStyle({ ...items[0], closure_status: 'blocked' }, false, false, false, 1).color, colors[1])
})

test('named destinations follow their categories through reordering and only fixed choices enter the request', () => {
  const choices = { '生鲜采买': { place_id: 'store', entry_id: 'store-gate' }, '基础教育': { place_id: 'school', entry_id: 'west-gate' } }
  const original = JSON.stringify(choices)
  assert.deepEqual(tripDraftPins(['基础教育', '医药', '生鲜采买'], choices), [
    { index: 0, ...choices['基础教育'] }, { index: 2, ...choices['生鲜采买'] },
  ])
  const fixed = tripDraftPins(['医药', '生鲜采买'], choices)
  const request = planRequestFor({ kind: 'create', origin, stops: ['医药', '生鲜采买'], fixed })
  assert.deepEqual(request.fixed_stops, [{ index: 1, ...choices['生鲜采买'] }])
  request.fixed_stops[0].entry_id = 'mutated'
  assert.equal(fixed[0].entry_id, 'store-gate')
  assert.equal(JSON.stringify(choices), original)
  assert.deepEqual(tripDraftPins(['医药'], choices), [])
})

test('a draft response must keep every explicitly selected facility, gate and station order', () => {
  const fixed = [{ index: 1, place_id: 'school', entry_id: 'school' }]
  assert.equal(tripDraftIssue(plan.stops, fixed, plan), null)
  assert.match(tripDraftIssue(plan.stops, fixed, { ...plan, legs: [items[0]] }), /第 2 站/)
  assert.match(tripDraftIssue(plan.stops, fixed, { ...plan, legs: [items[0], { ...items[1], entry_id: 'east-gate' }] }), /入口/)
  assert.match(tripDraftIssue(plan.stops, fixed, { ...plan, stops: [...plan.stops].reverse() }), /站序/)
})

test('arrows follow original forward, reversed and densely sampled polylines', () => {
  const east = routeArrowPositions([{ x: 0, y: 0 }, { x: 300, y: 0 }], 100)
  assert.deepEqual(east.map(p => [p.x, p.y, p.angle]), [[50, 0, 0], [150, 0, 0], [250, 0, 0]])
  const west = routeArrowPositions([{ x: 300, y: 0 }, { x: 0, y: 0 }], 100)
  assert.equal(west[0].angle, Math.PI)
  const dense = routeArrowPositions(Array.from({ length: 301 }, (_, x) => ({ x, y: 0 })), 100)
  assert.deepEqual(dense, east)
  const turn = routeArrowPositions([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 200 }], 100)
  assert.equal(turn[1].angle, Math.PI / 2)
})

test('overlapping routes select the active segment; clicking away does not select any route', () => {
  const overlap = { ...items[0], entry_id: 'return', route: { ...items[0].route, path: [...items[0].route.path].reverse() } }
  const project = ([lng, lat]) => ({ x: (lng - origin.lng) * 100000, y: (lat - origin.lat) * 100000 })
  assert.equal(hitTripRoute([items[0], overlap], 'return', { x: 200, y: 3 }, project), 'return')
  assert.equal(hitTripRoute(items, null, { x: -100, y: -100 }, project), null)
  assert.equal(hitTripRoute(items, null, { x: 200, y: 20 }, project), null)
})

test('a fully usable normal response starts walking, independently of optimality proof', () => {
  assert.equal(itineraryActions({ ...plan, optimality: 'candidate' }, null).primary.kind, 'journey')
  const focused = itineraryActions(plan, 'school')
  assert.equal(focused.primary.kind, 'segment')
  assert.equal(focused.primary.item.entry_id, 'school')
  assert.equal(focused.secondary.kind, 'journey')
  const { routing_status, ...legacy } = plan
  assert.match(itineraryGuideIssue(legacy), /状态数据不完整/)
  assert.equal(itineraryActions(legacy, null).primary.kind, 'retry')
})

test('missing second leg geometry gives a targeted retry and keeps a first-leg preview', () => {
  const partial = { ...plan, routing_status: 'unverified', legs: [items[0], { ...items[1], route: null, closure_status: 'unverified' }] }
  const actions = itineraryActions(partial, null)
  assert.equal(actions.primary.kind, 'retry')
  assert.match(actions.primary.label, /第 2 段/)
  assert.equal(actions.secondary.item.entry_id, 'store')
  assert.deepEqual(itineraryPath(partial.legs[1]), [])
  const project = ([lng, lat]) => ({ x: (lng - origin.lng) * 100000, y: (lat - origin.lat) * 100000 })
  const midpoint = project([121.4325, 31.255])
  assert.equal(hitTripRoute(partial.legs, null, midpoint, project), null)
})

test('quota or an HTTP retry restriction shows known routes instead of an endless retry button', () => {
  const partial = { ...plan, routing_status: 'unverified', legs: [items[0], { ...items[1], route: null, closure_status: 'unverified', note: '步行路线天配额耗尽，次日恢复。' }] }
  assert.match(itineraryRetryIssue(partial), /配额/)
  assert.equal(itineraryActions(partial, null).primary.kind, 'segment')
  const temporary = { ...partial, legs: [items[0], { ...partial.legs[1], note: null }] }
  assert.equal(itineraryActions(temporary, null, '小时请求次数已达上限').primary.kind, 'segment')
  const blocked = { ...temporary, routing_status: 'blocked', legs: [items[0], { ...items[1], closure_status: 'blocked' }] }
  assert.equal(itineraryActions(blocked, null).primary.kind, 'segment')
  assert.equal(itineraryActions(blocked, null).secondary.kind, 'edit')
})

test('retry input fixes every station and gate; replacement retry retains the exact failed choice', () => {
  const before = JSON.stringify(plan)
  const choice = { entry_id: 'new-gate', place_id: 'new-school' }
  const failed = { kind: 'replace', origin, plan, index: 1, selection: choice }
  const request = planRequestFor(failed)
  assert.deepEqual(request.replace_stop, { index: 1, ...choice })
  assert.deepEqual(request.selected_stops, items.map(({ entry_id, place_id }) => ({ entry_id, place_id })))
  const retry = planRequestFor({ kind: 'retry', origin, plan })
  assert.equal(retry.retry_routes, true)
  assert.equal(retry.replace_stop, undefined)
  assert.deepEqual(retry.selected_stops, request.selected_stops)
  const draft = planRequestFor({ kind: 'create', origin, stops: plan.stops })
  draft.origin.lat += 1; draft.stops.reverse()
  assert.equal(JSON.stringify(plan), before)
})

test('clear flags cannot enable a journey with a disconnected intermediate school gate', () => {
  const disconnected = { ...plan, legs: [items[0], { ...items[1], from: { ...items[1].from, lng: 121.427 } }] }
  assert.match(itineraryGuideIssue(disconnected), /不连续/)
  assert.notEqual(itineraryActions(disconnected, null).primary.kind, 'journey')
})

test('updating a usable journey preserves the original when a new draft returns degraded legs', () => {
  const original = JSON.stringify(plan)
  const partial = { ...plan, routing_status: 'unverified', legs: [items[0], { ...items[1], route: null, closure_status: 'unverified', note: '第二段路线请求失败' }] }
  assert.match(tripUpdateIssue(plan, partial), /失败.*原行程已保留/)
  assert.equal(tripUpdateIssue(undefined, partial), null)
  assert.equal(tripUpdateIssue(partial, plan), null)
  assert.equal(tripUpdateIssue(plan, plan), null)
  assert.equal(JSON.stringify(plan), original)
})
