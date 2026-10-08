import type { TripItem, TripMapData } from '../types'
import { PLACE_MARK } from '../components/map/context'

export function tripMeters(value: number) { return `${Math.round(value)} 米` }
export function tripMinutes(value: number) { return `${Math.max(1, Math.ceil(value / 60))} 分钟` }
export function tripColor(category: string) { return PLACE_MARK[category]?.color ?? '#1c3a28' }
export function tripPath(item: TripItem): [number, number][] {
  return item.route?.path ?? [[item.from.lng, item.from.lat], [item.lng, item.lat]]
}
export function tripBounds(data: TripMapData): [number, number][] {
  return [[data.origin.lng, data.origin.lat], ...data.items.flatMap(tripPath)]
}
export function tripVerification(result: { verification_basis?: string | null; lower_bound_slack_m?: number }) {
  if (result.verification_basis === 'all_snapshot_candidates') return '全部已检索候选已测清'
  if (result.verification_basis === 'lower_bound_slack_assumption') return `在 ${result.lower_bound_slack_m ?? 30} 米下界容差假设下已验证`
  return '尚未完成全部候选验证'
}
