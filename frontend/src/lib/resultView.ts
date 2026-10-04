import type { IsochroneFeature } from '../types'

/**
 * 「纯算法 / 含标注」两种看法。
 *
 * 页面上的结果是叠加了标注的 B；后端在 properties.markings.baseline 里附带了还原纯算法结果 A
 * 所需的差异（变了的格子、设施点、灰色区域，以及围挡改了圈时的内外圈）。
 * 这里把 B 按差异改回 A，地图即时切换，不重算、不花配额。
 */

export type ResultView = 'markings' | 'algorithm'

export type ViewSwitch =
  /** 没有附近标注，或这份结果不含标注信息：不显示切换 */
  | { kind: 'none' }
  /** 叠加的标注改变了结果，且带了还原差异：即时切换 */
  | { kind: 'instant' }
  /** 这次是按纯算法算的，或较早保存的结果没有差异：切换要重算（大部分命中缓存） */
  | { kind: 'rerun'; to: 'auto' | 'none' }

const cellKey = (lat: number, lng: number) => `${lat.toFixed(5)},${lng.toFixed(5)}`

export function viewSwitch(feature: IsochroneFeature | null): ViewSwitch {
  const m = feature?.properties.markings
  if (!feature || !m || feature.properties.simulated) return { kind: 'none' }
  if (m.mode === 'none') return m.nearby_count > 0 ? { kind: 'rerun', to: 'auto' } : { kind: 'none' }
  // 没有叠加任何会改变输入的标注：两种看法完全一样
  if (!m.baseline) return { kind: 'none' }
  if (m.baseline.cells_changed === undefined) return { kind: 'rerun', to: 'none' }
  return { kind: 'instant' }
}

/** 把叠加了标注的结果还原成纯算法结果（只用于地图显示）。 */
export function algorithmView(feature: IsochroneFeature): IsochroneFeature {
  const p = feature.properties
  const base = p.markings?.baseline
  if (!base || base.cells_changed === undefined) return feature

  const blindspots = p.blindspots
    ? (() => {
        const changed = new Map(base.cells_changed!.map((c) => [cellKey(c.lat, c.lng), c]))
        const cells = p.blindspots.cells.map((c) => changed.get(cellKey(c.lat, c.lng)) ?? c)
        return {
          ...p.blindspots,
          cells,
          blind_count: cells.filter((c) => c.missing.length > 0).length,
        }
      })()
    : p.blindspots

  return {
    ...feature,
    geometry: base.ring ? { ...feature.geometry, coordinates: [base.ring] } : feature.geometry,
    properties: {
      ...p,
      rings: base.ring && base.inner_rings ? base.inner_rings : p.rings,
      raw_ring: base.ring && base.raw_ring ? base.raw_ring : p.raw_ring,
      blindspots,
      coverage: p.coverage && base.places ? { ...p.coverage, places: base.places } : p.coverage,
      report:
        p.report && base.regions !== undefined ? { ...p.report, gray_regions: base.regions } : p.report,
    },
  }
}
