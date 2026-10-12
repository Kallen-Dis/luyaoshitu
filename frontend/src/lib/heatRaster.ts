/**
 * 把网格耗时铺成连续色场。
 *
 * 命题要的是「等时圈热力图」，方块拼出来的观感太糙。但现成的热力图控件（含 MapVGL
 * 的 HeatmapLayer）画的是**密度**：强度由核函数按点的疏密叠加，还会晕到采样范围之外。
 * 用它来表达「步行耗时」会得出两个错误读数——边界处点少所以显得「近」，
 * 等时圈外没测过的地方也被染上颜色。
 *
 * 这里改成插值，而且插的是**耗时**，不是颜色：
 *
 * 1. 网格耗时放进规则点阵，向外补两圈（取相邻实测格的平均），色场才能铺满到圈边，
 *    不会在最外一排格心之外渐隐成半透明；测距失败的格不补，留空；
 * 2. 每格细分成 8×8 个像素做双线性插值，再做两遍二项式平滑，消掉格线上的折痕；
 * 3. 最后才按色阶上色。直接放大一格一像素的彩色小图时，浏览器在 RGB 空间插值，
 *    绿和红之间会插出发灰的褐色，色场看起来脏。
 *
 * 色阶按时间阈值定死（默认 15 分钟），不随样例最远的一格伸缩：同一种颜色在两个样例里是同一个耗时，
 * 阈值落在橙色，超过阈值三分之一就是红色。颜色只在实测网格之间过渡，配合等时圈裁剪，
 * 不会在没测过的位置铺出结论。
 */

/** 热力色阶：由近及远。与图例共用一份定义。 */
export const HEAT_COLORS = ['#1a9850', '#66bd63', '#d9ef8b', '#fee08b', '#fdae61', '#d73027']

/** 各色在「耗时 ÷ 阈值」上的位置：阈值处是浅橙，阈值的 4/3 倍（15 分钟阈值即 20 分钟）及以上是红色。 */
export const HEAT_STOPS = [0, 1 / 3, 2 / 3, 0.9, 1, 4 / 3]

/** 色阶的上端（阈值的倍数）。 */
export const HEAT_MAX = HEAT_STOPS[HEAT_STOPS.length - 1]

const RGB = HEAT_COLORS.map((hex) => [
  parseInt(hex.slice(1, 3), 16),
  parseInt(hex.slice(3, 5), 16),
  parseInt(hex.slice(5, 7), 16),
])

/** 每格细分的像素数 */
const UPSAMPLE = 8
/** 色场向实测格之外补几圈：圈边离最外一排格心最多约一格 */
const PAD = 2

export interface HeatCell {
  lat: number
  lng: number
  reach_s: number | null
}

export interface HeatTile {
  image: HTMLCanvasElement
  south: number
  west: number
  north: number
  east: number
}

/** 耗时 → 颜色。t 是「耗时 ÷ 阈值」。 */
export function rampColor(t: number): [number, number, number] {
  const x = Math.max(0, Math.min(HEAT_MAX, t))
  let i = 0
  while (i < HEAT_STOPS.length - 2 && x > HEAT_STOPS[i + 1]) i++
  const span = HEAT_STOPS[i + 1] - HEAT_STOPS[i]
  const f = span > 0 ? (x - HEAT_STOPS[i]) / span : 0
  const a = RGB[i]
  const b = RGB[i + 1]
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ]
}

/** 单格的热力颜色。3D 视角下色场画布对不上透视，改按方格逐个着色时用它。 */
export function heatColor(valueS: number, targetS: number): string {
  const [r, g, b] = rampColor(targetS > 0 ? valueS / targetS : 0)
  return `rgb(${r}, ${g}, ${b})`
}

/** 图例用的 CSS 渐变：横轴是 0 到色阶上端，和地图上同一套颜色位置。 */
export function heatGradientCss(): string {
  const stops = HEAT_COLORS.map((c, i) => `${c} ${((HEAT_STOPS[i] / HEAT_MAX) * 100).toFixed(1)}%`)
  return `linear-gradient(to right, ${stops.join(', ')})`
}

/**
 * 规则点阵上的耗时场：rows × cols，NaN 表示没有值。
 * 测距失败的格（reach_s 为 null）记为 blocked，向外补值时不填它们。
 */
export function heatField(
  cells: readonly HeatCell[],
  spacingM: number,
): { values: Float64Array; rows: number; cols: number; latMin: number; lngMin: number; dLat: number; dLng: number } | null {
  const known = cells.filter((c) => c.reach_s !== null)
  if (known.length === 0) return null
  const latRef = known.reduce((acc, c) => acc + c.lat, 0) / known.length
  const dLat = spacingM / 111_320
  const dLng = spacingM / (111_320 * Math.cos((latRef * Math.PI) / 180))
  const latMin = Math.min(...cells.map((c) => c.lat)) - PAD * dLat
  const lngMin = Math.min(...cells.map((c) => c.lng)) - PAD * dLng
  const at = (c: HeatCell) => ({
    row: Math.round((c.lat - latMin) / dLat),
    col: Math.round((c.lng - lngMin) / dLng),
  })
  const placed = cells.map((c) => ({ ...at(c), value: c.reach_s }))
  const rows = Math.max(...placed.map((p) => p.row)) + 1 + PAD
  const cols = Math.max(...placed.map((p) => p.col)) + 1 + PAD
  const values = new Float64Array(rows * cols).fill(Number.NaN)
  const blocked = new Uint8Array(rows * cols)
  for (const { row, col, value } of placed) {
    if (value === null) blocked[row * cols + col] = 1
    else values[row * cols + col] = value
  }
  // 向外补 PAD 圈：空点取相邻（八邻接）已有值的平均。每圈基于上一圈的结果，逐圈外推
  for (let pass = 0; pass < PAD; pass++) {
    const prev = values.slice()
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const i = r * cols + c
        if (!Number.isNaN(prev[i]) || blocked[i]) continue
        let sum = 0
        let n = 0
        for (let dr = -1; dr <= 1; dr++) {
          for (let dc = -1; dc <= 1; dc++) {
            const rr = r + dr
            const cc = c + dc
            if (rr < 0 || cc < 0 || rr >= rows || cc >= cols || (dr === 0 && dc === 0)) continue
            const v = prev[rr * cols + cc]
            if (!Number.isNaN(v)) {
              sum += v
              n++
            }
          }
        }
        if (n > 0) values[i] = sum / n
      }
    }
  }
  return { values, rows, cols, latMin, lngMin, dLat, dLng }
}

/** 细分网格上的双线性插值：缺值的角不参与，按其余角的权重归一。 */
function upsample(values: Float64Array, rows: number, cols: number, f: number): Float64Array {
  const h = rows * f
  const w = cols * f
  const out = new Float64Array(h * w).fill(Number.NaN)
  for (let py = 0; py < h; py++) {
    const gy = (py + 0.5) / f - 0.5
    const r0 = Math.floor(gy)
    const fy = gy - r0
    for (let px = 0; px < w; px++) {
      const gx = (px + 0.5) / f - 0.5
      const c0 = Math.floor(gx)
      const fx = gx - c0
      let sum = 0
      let wsum = 0
      for (const [dr, wy] of [[0, 1 - fy], [1, fy]] as const) {
        const r = r0 + dr
        if (r < 0 || r >= rows || wy === 0) continue
        for (const [dc, wx] of [[0, 1 - fx], [1, fx]] as const) {
          const c = c0 + dc
          if (c < 0 || c >= cols || wx === 0) continue
          const v = values[r * cols + c]
          if (Number.isNaN(v)) continue
          sum += v * wy * wx
          wsum += wy * wx
        }
      }
      if (wsum > 0.05) out[py * w + px] = sum / wsum
    }
  }
  return out
}

/** 二项式 [1 4 6 4 1] 平滑，横竖各一遍；缺值不参与、也不被填上。 */
function smooth(values: Float64Array, h: number, w: number): Float64Array {
  const k = [1, 4, 6, 4, 1]
  const pass = (src: Float64Array, horizontal: boolean) => {
    const dst = new Float64Array(src.length).fill(Number.NaN)
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        const i = y * w + x
        if (Number.isNaN(src[i])) continue
        let sum = 0
        let wsum = 0
        for (let t = -2; t <= 2; t++) {
          const xx = horizontal ? x + t : x
          const yy = horizontal ? y : y + t
          if (xx < 0 || yy < 0 || xx >= w || yy >= h) continue
          const v = src[yy * w + xx]
          if (Number.isNaN(v)) continue
          sum += v * k[t + 2]
          wsum += k[t + 2]
        }
        dst[i] = sum / wsum
      }
    }
    return dst
  }
  return pass(pass(values, true), false)
}

/** 栅格化为一张小图（每格 8×8 像素），调用方按经纬度边界放大绘制即可得到平滑色场。 */
export function buildHeatTile(
  cells: readonly HeatCell[],
  spacingM: number,
  targetS: number,
  // 色场画在地图之上、盲区方格之下
  alpha = 0.5,
): HeatTile | null {
  if (targetS <= 0) return null
  const field = heatField(cells, spacingM)
  if (!field) return null
  const { rows, cols, latMin, lngMin, dLat, dLng } = field
  const f = UPSAMPLE
  const w = cols * f
  const h = rows * f
  const fine = smooth(smooth(upsample(field.values, rows, cols, f), h, w), h, w)

  const image = document.createElement('canvas')
  image.width = w
  image.height = h
  const ctx = image.getContext('2d')
  if (!ctx) return null
  const data = ctx.createImageData(w, h)
  const a = Math.round(alpha * 255)
  for (let py = 0; py < h; py++) {
    // 画布 y 轴向下，纬度向上，故行号要翻转
    const src = (h - 1 - py) * w
    for (let px = 0; px < w; px++) {
      const v = fine[src + px]
      if (Number.isNaN(v)) continue
      const [r, g, b] = rampColor(v / targetS)
      const idx = (py * w + px) * 4
      data.data[idx] = r
      data.data[idx + 1] = g
      data.data[idx + 2] = b
      data.data[idx + 3] = a
    }
  }
  ctx.putImageData(data, 0, 0)

  // 节点 (0, 0) 在左下角那一格的中心，图像边界外扩半格
  return {
    image,
    south: latMin - dLat / 2,
    north: latMin + (rows - 0.5) * dLat,
    west: lngMin - dLng / 2,
    east: lngMin + (cols - 0.5) * dLng,
  }
}
