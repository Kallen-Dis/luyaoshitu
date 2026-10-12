import type { LatLng } from '../../lib/markings'
import type {
  ConstructionCandidate,
  GrayRegion,
  GridCell,
  GridCheckClue,
  GridCheckGap,
  MarkingType,
  Place,
  RecheckSuspect,
} from '../../types'

/** 地图图层开关。15 分钟圈与临时围挡总是显示，不在这里。 */
export interface LayerState {
  /** 5 / 10 分钟内圈 */
  innerRings: boolean
  /** 步行耗时热力 */
  heat: boolean
  /** 走不进 15 分钟圈的设施 */
  outsidePlaces: boolean
  /** 盲区方格 */
  blind: boolean
  /** 灰色区域的外框与编号 */
  regions: boolean
  /** 附近的共享标注 */
  markings: boolean
  /** 复测疑似点与工地候选 */
  clues: boolean
  /** 网格交叉校核：射线圈里确认走不到的缺口（斜线） */
  checkGaps: boolean
  /** 网格交叉校核：校核估计范围的边界（另一种插值，不替代射线圈） */
  checkEstimate: boolean
  /** 网格交叉校核：绕行线索 */
  detourClues: boolean
}

/**
 * 默认只开最常用的几层：圈、设施、盲区方格、灰色区域、共享标注。
 * 网格交叉校核的三层（缺口、校核估计范围、绕行线索）都是对等时圈本身的核对，默认关：
 * 红色斜线是图上最醒目的符号，默认画着会抢走灰色区域（主结论）的视线，也容易被当成另一种盲区。
 */
export const DEFAULT_LAYERS: LayerState = {
  innerRings: true,
  heat: false,
  outsidePlaces: true,
  blind: true,
  regions: true,
  markings: true,
  clues: true,
  checkGaps: false,
  checkEstimate: false,
  detourClues: false,
}

/** 用户在地图上点到的东西。弹出卡片按它给出能做的事。 */
export type MapContext =
  | { kind: 'cell'; cell: GridCell; missing: string[] }
  | { kind: 'region'; region: GrayRegion }
  | { kind: 'place'; place: Place }
  | { kind: 'suspect'; suspect: RecheckSuspect }
  | { kind: 'site'; site: ConstructionCandidate }
  | { kind: 'gap'; gap: GridCheckGap; clues: GridCheckClue[]; targetS: number }
  | { kind: 'clue'; clue: GridCheckClue }
  | { kind: 'point'; lat: number; lng: number }

/** 从地图上点进标注时预先带好的内容。 */
export interface ComposerPreset {
  sellsVegetables?: boolean
  type: MarkingType
  source?: 'user' | 'recheck' | 'poi' | 'agent_plan'
  point?: LatLng | null
  radius?: number
  place?: Place | null
  vertices?: LatLng[]
  /** 补录设施的类别 */
  category?: string
  /** 补录设施的名称（AI 二次核对找到的设施带着名字进来） */
  name?: string
  /** 灰色区域缺的类别 */
  categories?: string[]
  /** 设施失效 / 灰色区域的原因 */
  reason?: string
  /** 抽屉顶部的小字：从哪里点进来的 */
  context?: string
}

/** 弹出卡片里点了某个动作。 */
export type MapIntent =
  | { kind: 'trip-to'; place: Place }
  | { kind: 'trip-from-cell'; cell: GridCell; category: string }
  | { kind: 'compose'; preset: ComposerPreset }
  | { kind: 'temp-closure'; key: string; lat: number; lng: number; radius: number; label: string }
  | { kind: 'dismiss'; key: string }
  | { kind: 'select-marking'; id: number }

/** 命题点名的三类设施在卡片里用的短名。 */
export const SHORT_NAME: Record<string, string> = {
  生鲜采买: '菜场',
  医药: '药店',
  基础教育: '小学',
  基础医疗: '卫生服务点',
  养老服务: '养老设施',
  文体休闲: '文体设施',
}

/**
 * 盲区按品类分色。同时看三类会糊成一片，故一次只画一层。
 * 「缺任一类」就是命题里的「设施匮乏灰色区域」，用灰色；单看某一类时用该类的颜色。
 */
/** 网格交叉校核的配色：缺口暗红斜线，绕行线索赭褐，校核估计范围靛蓝虚线。
 *  与施工围挡的亮红、灰色区域的深灰、等时圈的绿色都区分开。 */
export const CHECK_GAP_COLOR = '#9f1239'
export const CHECK_CLUE_COLOR = '#92400e'
export const CHECK_ESTIMATE_COLOR = '#4338ca'

export const BLIND_COLORS: Record<string, string> = {
  all: '#6b7280',
  生鲜采买: '#ea580c',
  医药: '#dc2626',
  基础教育: '#7c3aed',
}

/**
 * 热力色阶按时间阈值定死（默认 15 分钟 = 900 秒），不随这份结果最远的一格伸缩：
 * 同一种颜色在两个样例里是同一个耗时；阈值落在浅橙，阈值的 4/3 倍及以上是红色（见 heatRaster.ts）。
 */
export function heatTargetS(minutes?: number): number {
  return (minutes ?? 15) * 60
}

export const PLACE_MARK: Record<string, { glyph: string; color: string }> = {
  生鲜采买: { glyph: '菜', color: '#c05621' },
  医药: { glyph: '药', color: '#2f7d32' },
  基础教育: { glyph: '学', color: '#1d4e89' },
  基础医疗: { glyph: '医', color: '#0f766e' },
  养老服务: { glyph: '养', color: '#7c3aed' },
  文体休闲: { glyph: '文', color: '#b45309' },
}
