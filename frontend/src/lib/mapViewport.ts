import type { IsochroneFeature } from '../types'

/** 图层/报告更新不改变视野；主范围或比较范围改变才重新适配。 */
export function overviewViewportKey(feature: IsochroneFeature | null | undefined): string {
  return feature ? JSON.stringify([feature.properties.center, feature.geometry]) : ''
}
