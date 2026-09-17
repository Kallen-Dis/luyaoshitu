/** 后端 /api 返回的数据结构。 */

export interface IsochroneProperties {
  minutes: number
  area_m2: number
  area_km2: number
  mean_radius_m: number
  min_radius_m: number
  max_radius_m: number
  /** 最短方向半径 ÷ 最远方向半径。明显偏低意味着存在铁路、河道等可达性切割。 */
  compactness: number
  mean_detour?: number | null
  max_detour?: number | null
  rays?: RayMetric[]
  report?: ExamReport
  coverage?: Coverage
  blindspots?: Blindspots
  sampled_points: number
  /** 算路失败的采样点数。非零时结果偏保守，需在界面上提示。 */
  failed_points: number
  name?: string
  center?: { lat: number; lng: number }
  generated_at?: string
}

export interface IsochroneFeature {
  type: 'Feature'
  geometry: {
    type: 'Polygon'
    /** GeoJSON 惯例为 [经度, 纬度]，与百度 API 的 (lat, lng) 顺序相反。 */
    coordinates: [number, number][][]
  }
  properties: IsochroneProperties
}

export interface SampleMeta {
  id: string
  name: string
  minutes: number
  center: { lat: number; lng: number }
  area_km2: number
  mean_radius_m: number
  compactness: number
  generated_at: string | null
  grade: string | null
  total: number | null
  /** 真实可达面积 ÷ 直线圆面积。越低说明直线法高估越严重。 */
  area_ratio: number | null
  facilities_in: number | null
  facilities_nearby: number | null
}

export interface CategoryMeta {
  name: string
  /** 命题点名的三类设施（菜市场、药店、小学），盲区判定只对它们逐网格测距。 */
  key_facility: boolean
}

export interface AppConfig {
  browser_ak: string
  default_minutes: number
  default_directions: number
  categories: CategoryMeta[]
  walk_limit_m: number
}

export interface RayMetric {
  bearing: number
  radius_m: number
  detour: number | null
  barrier: boolean
}

/** 清洗过程的留存与剔除计数，用于说明数据质量。 */
export interface CleanStats {
  raw: number
  invalid: number
  excluded: number
  out_of_range: number
  duplicated: number
  dropped: number
}

export interface Coverage {
  /** 等时圈**内**的设施数。 */
  categories: Record<string, number>
  /** 检索半径内的设施数。与 categories 的差额即「在附近但走不进圈里」。 */
  nearby_categories: Record<string, number>
  /** 检索失败的品类。数量未知，绝不可当作零。 */
  failed_categories: string[]
  searches: number
  radius_m: number
  clean_stats: Record<string, CleanStats>
  source?: string
}

/** 一个网格点的判定结果。热力图与盲区标注共用它。 */
export interface GridCell {
  lat: number
  lng: number
  /** 中心点步行到此的耗时（秒），热力图强度由它决定。null 表示测距失败。 */
  reach_s: number | null
  nearest_m: Record<string, number | null>
  /** 步行 1 公里内确无此类设施 */
  missing: string[]
  /** 测距失败，既不能判有也不能判无 */
  unknown: string[]
}

export interface Blindspots {
  grid_spacing_m: number
  walk_limit_m: number
  cell_count: number
  blind_count: number
  max_reach_s: number | null
  blind_ratio: Record<string, number>
  cells: GridCell[]
}

export interface ExamReport {
  total: number
  grade: string
  dimensions: {
    reach: number
    compact: number
    detour: number | null
    cover: number | null
    equity: number | null
  }
  blinds: string[]
  ideal_area_km2: number
  area_ratio: number
  straight_inflation: number | null
  categories: Record<string, number> | null
  failed_categories: string[]
  blind_ratio: Record<string, number> | null
  blind_cell_count: number | null
  cell_count: number | null
  coverage_source: string | null
  coverage_pending: boolean
  blindspots_pending: boolean
}

export interface ApiErrorDetail {
  code: string
  message: string
  status?: number
}
