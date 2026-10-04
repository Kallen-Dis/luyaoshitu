import type { Blindspots, GridCell, SimulationResult } from '../types'

/**
 * 盲区方格的前端读法。判定全部来自后端的真实路网结果（blindspot.py），
 * 这里只做两件事：按品类筛选，以及把「模拟新建」消去的方格从缺失里拿掉。
 * 不在前端重新计算任何距离——直线距离会把「直线够近、走路绕远」的居民点漏判成有覆盖。
 */

const key = (lat: number, lng: number) => `${lat.toFixed(5)},${lng.toFixed(5)}`

function coveredKeys(simulation?: SimulationResult | null): Set<string> {
  return new Set((simulation?.covered_cells ?? []).map((c) => key(c.lat, c.lng)))
}

/** 叠加模拟新建后，这一格还缺哪些品类。 */
export function effectiveMissing(
  cell: GridCell,
  simulation?: SimulationResult | null,
  covered: Set<string> = coveredKeys(simulation),
): string[] {
  if (!simulation || !covered.has(key(cell.lat, cell.lng))) return cell.missing
  return cell.missing.filter((m) => m !== simulation.category)
}

export interface GridCensus {
  total: number
  blind: number
  inCircle: number
  blindInCircle: number
  /** 品类 -> 缺该品类的格数 */
  missing: Record<string, number>
}

export function gridCensus(
  blindspots?: Blindspots | null,
  simulation?: SimulationResult | null,
): GridCensus | null {
  const cells = blindspots?.cells ?? []
  if (cells.length === 0) return null
  const covered = coveredKeys(simulation)
  const missing: Record<string, number> = {}
  let blind = 0
  let inCircle = 0
  let blindInCircle = 0
  for (const cell of cells) {
    const miss = effectiveMissing(cell, simulation, covered)
    const inside = cell.in_circle ?? true
    if (inside) inCircle += 1
    if (miss.length > 0) {
      blind += 1
      if (inside) blindInCircle += 1
    }
    for (const name of miss) missing[name] = (missing[name] ?? 0) + 1
  }
  return { total: cells.length, blind, inCircle, blindInCircle, missing }
}

/** 按品类筛出要画的盲区方格（'all' 表示缺任一类）。 */
export function blindCells(
  blindspots: Blindspots | null | undefined,
  category: string,
  simulation?: SimulationResult | null,
): { cell: GridCell; missing: string[] }[] {
  const covered = coveredKeys(simulation)
  const out: { cell: GridCell; missing: string[] }[] = []
  for (const cell of blindspots?.cells ?? []) {
    // 灰色区域只标 15 分钟圈内（后端打开较早的结果时已收成圈内，这里再守一道）
    if (cell.in_circle === false) continue
    const miss = effectiveMissing(cell, simulation, covered)
    if (category === 'all' ? miss.length > 0 : miss.includes(category)) {
      out.push({ cell, missing: miss })
    }
  }
  return out
}

/** 模拟新建在当前图层上消去了多少格。 */
export function erasedCount(
  blindspots: Blindspots | null | undefined,
  simulation: SimulationResult | null,
  category: string,
): number {
  if (!simulation) return 0
  const before = blindCells(blindspots, category).length
  const after = blindCells(blindspots, category, simulation).length
  return Math.max(0, before - after)
}
