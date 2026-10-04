/**
 * 把网格耗时铺成连续色场。
 *
 * 命题要的是「等时圈热力图」，方块拼出来的观感太糙。但现成的热力图控件（含 MapVGL
 * 的 HeatmapLayer）画的是**密度**：强度由核函数按点的疏密叠加，还会晕到采样范围之外。
 * 用它来表达「步行耗时」会得出两个错误读数——边界处点少所以显得「近」，
 * 等时圈外没测过的地方也被染上颜色。
 *
 * 这里改成插值：先把规则点阵栅格化成一张「一格一像素」的小图，再放大绘制，
 * 由浏览器做双线性插值。颜色只在实测网格之间过渡，配合等时圈裁剪，
 * 不会在没测过的位置铺出结论。
 */

/** 热力色阶：由近及远。与图例共用一份定义。 */
export const HEAT_COLORS = ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027']

const RGB = HEAT_COLORS.map((hex) => [
  parseInt(hex.slice(1, 3), 16),
  parseInt(hex.slice(3, 5), 16),
  parseInt(hex.slice(5, 7), 16),
])

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

function rampColor(t: number): [number, number, number] {
  const x = Math.max(0, Math.min(1, t)) * (RGB.length - 1)
  const i = Math.min(RGB.length - 2, Math.floor(x))
  const f = x - i
  const a = RGB[i]
  const b = RGB[i + 1]
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ]
}

/** 单格的热力颜色。3D 视角下色场画布对不上透视，改按方格逐个着色时用它。 */
export function heatColor(value: number, maxReachS: number): string {
  const [r, g, b] = rampColor(maxReachS > 0 ? value / maxReachS : 0)
  return `rgb(${r}, ${g}, ${b})`
}

/** 栅格化为一格一像素的小图，调用方按经纬度边界放大绘制即可得到平滑色场。 */
export function buildHeatTile(
  cells: readonly HeatCell[],
  spacingM: number,
  maxReachS: number,
  // 色场画在地图之上，盲区轮廓在它之下，透明度过高会把轮廓压没
  alpha = 0.52,
): HeatTile | null {
  const known = cells.filter((c) => c.reach_s !== null)
  if (known.length === 0 || maxReachS <= 0) return null

  const latRef = known.reduce((acc, c) => acc + c.lat, 0) / known.length
  const dLat = spacingM / 111_320
  const dLng = spacingM / (111_320 * Math.cos((latRef * Math.PI) / 180))
  const latMin = Math.min(...known.map((c) => c.lat))
  const lngMin = Math.min(...known.map((c) => c.lng))

  const placed = known.map((c) => ({
    row: Math.round((c.lat - latMin) / dLat),
    col: Math.round((c.lng - lngMin) / dLng),
    value: c.reach_s as number,
  }))
  const rows = Math.max(...placed.map((p) => p.row)) + 1
  const cols = Math.max(...placed.map((p) => p.col)) + 1

  const image = document.createElement('canvas')
  image.width = cols
  image.height = rows
  const ctx = image.getContext('2d')
  if (!ctx) return null

  const data = ctx.createImageData(cols, rows)
  for (const { row, col, value } of placed) {
    // 画布 y 轴向下，纬度向上，故行号要翻转
    const idx = ((rows - 1 - row) * cols + col) * 4
    const [r, g, b] = rampColor(value / maxReachS)
    data.data[idx] = r
    data.data[idx + 1] = g
    data.data[idx + 2] = b
    data.data[idx + 3] = Math.round(alpha * 255)
  }
  ctx.putImageData(data, 0, 0)

  // 像素中心对应网格中心，故图像边界要外扩半格
  return {
    image,
    south: latMin - dLat / 2,
    north: latMin + (rows - 0.5) * dLat,
    west: lngMin - dLng / 2,
    east: lngMin + (cols - 0.5) * dLng,
  }
}
