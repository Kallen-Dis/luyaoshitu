import type { Blindspots, GridCell, SimulationResult } from '../types'

/**
 * 盲区方格的前端读法。判定全部来自后端的真实路网结果（blindspot.py），
 * 这里只做三件事：把 50 米细分过的格展开成 4 个子格，按品类筛选，
 * 以及把「模拟新建」消去的方格从缺失里拿掉。
 * 不在前端重新计算任何距离——直线距离会把「直线够近、走路绕远」的居民点漏判成有覆盖。
 *
 * 计数按 100 米格折算：子格各占 1/4，和父格不重复统计。
 */

const key = (lat: number, lng: number) => `${lat.toFixed(5)},${lng.toFixed(5)}`

function coveredKeys(simulation?: SimulationResult | null): Set<string> {
  return new Set((simulation?.covered_cells ?? []).map((c) => key(c.lat, c.lng)))
}

/** 地图上画的一块：没细分的 100 米格，或细分过的格里的一个 50 米子格。 */
export interface GridUnit {
  cell: GridCell
  size_m: number
  /** 占 100 米格的几分之几 */
  weight: number
}

/**
 * 细分过的格展开成子格；子格沿用父格的耗时，判定用子格自己的（未实测的品类继承父格）。
 * 子格中心落在射线圈外的（in_circle === false）不画、不计，与 100 米格「格心在圈内才算」同一条规则。
 */
export function gridUnits(blindspots?: Blindspots | null): GridUnit[] {
  const spacing = blindspots?.grid_spacing_m ?? 100
  const out: GridUnit[] = []
  for (const cell of blindspots?.cells ?? []) {
    if (!cell.quarters?.length) {
      out.push({ cell, size_m: spacing, weight: 1 })
      continue
    }
    const { quarters: _quarters, ...parent } = cell
    void _quarters
    for (const q of cell.quarters) {
      if (q.in_circle === false) continue
      out.push({
        cell: {
          ...parent,
          lat: q.lat,
          lng: q.lng,
          nearest_m: q.nearest_m,
          missing: q.missing,
          unknown: q.unknown,
          unknown_reasons: q.unknown_reasons,
          sub: { q: q.q, measured: q.measured, size_m: spacing / 2 },
        },
        size_m: spacing / 2,
        weight: 0.25,
      })
    }
  }
  return out
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
  /** 100 米格数（父格） */
  total: number
  /** 以下都按 100 米格折算，细分出的子格各 1/4，可能带小数 */
  /** 参与计数的总面积：细分过的格只算圈内子格，所以可能比 total 略小 */
  units: number
  blind: number
  inCircle: number
  blindInCircle: number
  /** 品类 -> 缺该品类的格数 */
  missing: Record<string, number>
  unknown: Record<string, number>
  /** 有没有做 50 米细分 */
  refined: boolean
}

export function gridCensus(
  blindspots?: Blindspots | null,
  simulation?: SimulationResult | null,
): GridCensus | null {
  const cells = blindspots?.cells ?? []
  if (cells.length === 0) return null
  const covered = coveredKeys(simulation)
  const missing: Record<string, number> = {}
  const unknown: Record<string, number> = {}
  let units = 0
  let blind = 0
  let inCircle = 0
  let blindInCircle = 0
  let refined = false
  for (const { cell, weight } of gridUnits(blindspots)) {
    if (cell.sub) refined = true
    units += weight
    const miss = effectiveMissing(cell, simulation, covered)
    const inside = cell.in_circle ?? true
    if (inside) inCircle += weight
    if (miss.length > 0) {
      blind += weight
      if (inside) blindInCircle += weight
    }
    for (const name of cell.unknown) unknown[name] = (unknown[name] ?? 0) + weight
    for (const name of miss) missing[name] = (missing[name] ?? 0) + weight
  }
  return { total: cells.length, units, blind, inCircle, blindInCircle, missing, unknown, refined }
}

/** 格数的写法：整数不带小数，细分出的 1/4 格保留到 0.25。 */
export function cellCount(n: number): string {
  return String(Math.round(n * 4) / 4)
}

/** 按品类筛出要画的盲区方格（'all' 表示缺任一类）。细分过的格按子格分别给。 */
export function blindCells(
  blindspots: Blindspots | null | undefined,
  category: string,
  simulation?: SimulationResult | null,
): { cell: GridCell; missing: string[]; size_m: number; weight: number }[] {
  const covered = coveredKeys(simulation)
  const out: { cell: GridCell; missing: string[]; size_m: number; weight: number }[] = []
  for (const { cell, size_m, weight } of gridUnits(blindspots)) {
    // 灰色区域只标 15 分钟圈内（后端打开较早的结果时已收成圈内，这里再守一道）
    if (cell.in_circle === false) continue
    const miss = effectiveMissing(cell, simulation, covered)
    if (category === 'all' ? miss.length > 0 : miss.includes(category)) {
      out.push({ cell, missing: miss, size_m, weight })
    }
  }
  return out
}

/** 一组盲区方格按 100 米格折算的格数。 */
export function blindArea(units: readonly { weight: number }[]): number {
  return units.reduce((sum, u) => sum + u.weight, 0)
}

/** 模拟新建在当前图层上消去了多少格（按 100 米格折算）。 */
export function erasedCount(
  blindspots: Blindspots | null | undefined,
  simulation: SimulationResult | null,
  category: string,
): number {
  if (!simulation) return 0
  const before = blindArea(blindCells(blindspots, category))
  const after = blindArea(blindCells(blindspots, category, simulation))
  return Math.max(0, before - after)
}
