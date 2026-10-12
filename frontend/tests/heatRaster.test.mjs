import assert from 'node:assert/strict'
import test from 'node:test'
import { HEAT_COLORS, heatColor, heatField, heatGradientCss, rampColor } from '../src/lib/heatRaster.ts'

const hex = (rgb) => `#${rgb.map((v) => v.toString(16).padStart(2, '0')).join('')}`

test('the colour scale is fixed to the time threshold, not to the farthest cell', () => {
  assert.equal(hex(rampColor(0)), HEAT_COLORS[0])
  assert.equal(hex(rampColor(1)), '#fdae61') // 15 分钟正好浅橙
  assert.equal(hex(rampColor(4 / 3)), HEAT_COLORS.at(-1)) // 20 分钟红
  assert.equal(hex(rampColor(3)), HEAT_COLORS.at(-1)) // 更远也只是红，不另起色
  // 同一个耗时在两份结果里同色：只看阈值
  assert.equal(heatColor(600, 900), heatColor(600, 900))
  assert.notEqual(heatColor(600, 900), heatColor(600, 1800))
})

test('interpolation runs on times, so green and red never meet as a muddy brown', () => {
  // 0 和 4/3 阈值的中点是 2/3 阈值：浅黄绿，而不是 RGB 中点那种发灰的颜色
  assert.equal(hex(rampColor(2 / 3)), HEAT_COLORS[2])
})

test('the field is padded outwards so the colour reaches the ring, but failed cells stay empty', () => {
  const cells = []
  for (let r = 0; r < 3; r++) {
    for (let c = 0; c < 3; c++) {
      cells.push({ lat: 31 + r * 0.0009, lng: 121 + c * 0.00105, reach_s: r === 1 && c === 1 ? null : 300 + 100 * c })
    }
  }
  const field = heatField(cells, 100)
  assert.ok(field)
  const { values, rows, cols } = field
  assert.equal(rows, 3 + 4)
  assert.equal(cols, 3 + 4)
  const at = (r, c) => values[r * cols + c]
  assert.ok(Number.isNaN(at(3, 3)), 'the failed centre cell is not filled in')
  assert.equal(at(2, 2), 300) // 实测格原样
  assert.ok(Number.isFinite(at(1, 2)) && Number.isFinite(at(0, 2)), 'two rings of padding below the grid')
  assert.ok(Number.isFinite(at(2, 0)) && Number.isFinite(at(2, 6)), 'and on both sides')
})

test('the legend gradient uses the same stop positions as the map', () => {
  const css = heatGradientCss()
  assert.match(css, /^linear-gradient\(to right, #1a9850 0\.0%/)
  assert.match(css, /#fdae61 75\.0%/) // 阈值在色条的 3/4 处，对应刻度「15」
  assert.match(css, /#d73027 100\.0%\)$/)
})
