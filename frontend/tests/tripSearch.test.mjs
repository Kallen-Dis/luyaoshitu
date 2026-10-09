import assert from 'node:assert/strict'
import test from 'node:test'
import { matchesTripQuery, normalizeTripQuery } from '../src/lib/tripSearch.ts'
import { NOTICE_DURATION_MS, scheduleNoticeDismiss } from '../src/lib/notice.ts'

test('facility filtering tolerates spaces, full-width characters, punctuation and case', () => {
  assert.equal(normalizeTripQuery(' ＡＢＣ（兰溪路店） '), 'abc兰溪路店')
  assert.ok(matchesTripQuery('振凯平价生鲜大卖场', [], '振凯 平价生鲜'))
  assert.ok(matchesTripQuery('ABC 药店（兰溪路店）', [], 'ａｂｃ 药店(兰溪路店)'))
  assert.ok(matchesTripQuery('菜市场', [], '   '))
})

test('Chinese shorthand and gate names match without matching an unrelated facility or merging fields', () => {
  assert.ok(matchesTripQuery('上海市朝春中心小学', ['东门', '西门'], '朝春小学'))
  assert.ok(matchesTripQuery('上海市朝春中心小学', ['东门', '西门'], '西门'))
  assert.ok(matchesTripQuery('振凯平价生鲜大卖场', [], '兰溪路店', ['振凯平价生鲜大卖场（兰溪路店）']))
  assert.equal(matchesTripQuery('上海市朝春中心小学', ['东门'], '武宁小学'), false)
  assert.equal(matchesTripQuery('药店', ['东门', '西门'], '东门西门'), false)
  assert.equal(matchesTripQuery('上海市朝春中心小学', [], '春学'), false)
})

test('normal guidance notices expire at eight seconds and canceled callbacks cannot dismiss a newer notice', () => {
  const jobs = [], canceled = []
  let oldDismissed = false, newDismissed = false
  const schedule = (callback, delay) => { jobs.push({ callback, delay }); return jobs.length }
  const cancel = id => canceled.push(id)
  const cleanup = scheduleNoticeDismiss(() => { oldDismissed = true }, schedule, cancel)
  assert.equal(jobs[0].delay, 8000)
  assert.equal(NOTICE_DURATION_MS, 8000)
  cleanup()
  scheduleNoticeDismiss(() => { newDismissed = true }, schedule, cancel)
  jobs[0].callback()
  assert.equal(oldDismissed, false)
  assert.equal(newDismissed, false)
  jobs[1].callback()
  assert.equal(newDismissed, true)
  assert.deepEqual(canceled, [1])
})
