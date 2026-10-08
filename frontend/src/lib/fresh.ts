import type { FreshEvidence, Place } from '../types'

export const freshLabel = (status: FreshEvidence['fresh_status']) => status === 'verified' ? '已确认卖菜' : status === 'inferred' ? '规则推定' : '是否卖菜待确认'
export const freshEligible = (place: Place) => place.category !== '生鲜采买' || (place.fresh_status !== 'pending' && place.fresh_status !== 'excluded')
