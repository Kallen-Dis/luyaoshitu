/**
 * 把判定为盲区的规则网格并成连片轮廓。
 *
 * 网格是判定单元——每个格子都单独做过真实路网测距——但棋盘格的画法会让人
 * 误以为盲区是一堆孤立方块。这里只做「并集的边界」：相邻两格共享的边互相抵消，
 * 剩下的边首尾相接就是外轮廓。面积严格等于被判定格子的并集，不做任何插值或平滑，
 * 因此不会在没测过的位置凭空画出结论。
 */

export interface LatLng {
  lat: number
  lng: number
}

export interface Ring {
  path: LatLng[]
  /** 环内空洞（被盲区包围的达标区域）。填充要留空，否则会把好的一片涂成坏的。 */
  hole: boolean
}

interface Lattice {
  originLat: number
  originLng: number
  dLat: number
  dLng: number
}

function latticeOf(cells: readonly LatLng[], spacingM: number): Lattice {
  const latRef = cells.reduce((acc, c) => acc + c.lat, 0) / cells.length
  // 经度方向的度距随纬度收缩，必须按纬度换算，否则格子对不齐会切出锯齿
  const dLat = spacingM / 111_320
  const dLng = spacingM / (111_320 * Math.cos((latRef * Math.PI) / 180))
  const latMin = Math.min(...cells.map((c) => c.lat))
  const lngMin = Math.min(...cells.map((c) => c.lng))
  return { originLat: latMin - dLat / 2, originLng: lngMin - dLng / 2, dLat, dLng }
}

/** 逆时针绕行时，格子的四条边。共享边成对出现、方向相反，恰好抵消。 */
function cellEdges(row: number, col: number): [string, string][] {
  const v = (r: number, c: number) => `${r},${c}`
  return [
    [v(row, col), v(row, col + 1)], // 下
    [v(row, col + 1), v(row + 1, col + 1)], // 右
    [v(row + 1, col + 1), v(row + 1, col)], // 上
    [v(row + 1, col), v(row, col)], // 左
  ]
}

function signedArea(points: [number, number][]): number {
  let sum = 0
  for (let i = 0; i < points.length; i += 1) {
    const [x1, y1] = points[i]
    const [x2, y2] = points[(i + 1) % points.length]
    sum += x1 * y2 - x2 * y1
  }
  return sum / 2
}

/** 去掉共线的中间点：一条直边不需要每 150 米插一个顶点。 */
function dropCollinear(points: [number, number][]): [number, number][] {
  if (points.length < 3) return points
  const out: [number, number][] = []
  for (let i = 0; i < points.length; i += 1) {
    const prev = points[(i - 1 + points.length) % points.length]
    const cur = points[i]
    const next = points[(i + 1) % points.length]
    const cross =
      (cur[0] - prev[0]) * (next[1] - cur[1]) - (cur[1] - prev[1]) * (next[0] - cur[0])
    if (cross !== 0) out.push(cur)
  }
  return out.length >= 3 ? out : points
}

export function dissolveCells(cells: readonly LatLng[], spacingM: number): Ring[] {
  if (cells.length === 0) return []

  const lattice = latticeOf(cells, spacingM)
  const occupied = new Set<string>()
  const index = cells.map((c) => ({
    row: Math.round((c.lat - lattice.originLat - lattice.dLat / 2) / lattice.dLat),
    col: Math.round((c.lng - lattice.originLng - lattice.dLng / 2) / lattice.dLng),
  }))
  for (const { row, col } of index) occupied.add(`${row},${col}`)

  // 边的起点 -> 若干终点。两格只在角上相碰时，一个顶点会有两条出边，故用数组
  const outgoing = new Map<string, string[]>()
  for (const { row, col } of index) {
    const neighbours = [
      occupied.has(`${row - 1},${col}`),
      occupied.has(`${row},${col + 1}`),
      occupied.has(`${row + 1},${col}`),
      occupied.has(`${row},${col - 1}`),
    ]
    cellEdges(row, col).forEach(([from, to], i) => {
      if (neighbours[i]) return // 与邻格共享，抵消
      const list = outgoing.get(from)
      if (list) list.push(to)
      else outgoing.set(from, [to])
    })
  }

  const rings: Ring[] = []
  for (const [start] of outgoing) {
    while ((outgoing.get(start)?.length ?? 0) > 0) {
      const loop: [number, number][] = []
      let cursor = start
      // 沿未走过的出边一路前进，回到起点即闭合一个环
      for (;;) {
        const nexts = outgoing.get(cursor)
        if (!nexts || nexts.length === 0) break
        const next = nexts.shift() as string
        const [r, c] = cursor.split(',').map(Number)
        loop.push([c, r])
        cursor = next
        if (cursor === start) break
      }
      if (loop.length < 4) continue

      const simplified = dropCollinear(loop)
      rings.push({
        hole: signedArea(simplified) < 0,
        path: simplified.map(([c, r]) => ({
          lat: lattice.originLat + r * lattice.dLat,
          lng: lattice.originLng + c * lattice.dLng,
        })),
      })
    }
  }
  return rings
}
