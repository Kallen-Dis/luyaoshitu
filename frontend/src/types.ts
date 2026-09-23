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
  /** 本次计算的输入坐标系（实时计算返回；非 bd09 时中心点已被转换）。 */
  input_coord_sys?: string
  generated_at?: string
  mode?: string
  mode_label?: string
  uses_traffic?: boolean
  speed_m_per_s?: number
  factors?: string[]
  blindspots_skipped?: string
  /** 等时圈算法质量诊断（实时计算返回完整值；快照由射线表推导，saturated 为 null）。 */
  quality?: IsochroneQuality
  /** 本次计算的配额消耗（仅实时计算返回；快照零消耗不携带）。 */
  quota?: QuotaStats
  /** 离线模拟标记：为 true 时结果由确定性伪随机生成，不代表真实路网。 */
  simulated?: boolean
  /** 同一次采样插出的内圈。快照若是旧格式则没有。 */
  rings?: { minutes: number; coordinates: [number, number][] }[]
}

/** 等时圈质量诊断：让用户知道哪些方向是截断/饱和，而不是把多边形当确定结果。 */
export interface IsochroneQuality {
  directions: number
  /** 采样上界内始终未超时的方向数；真实边界可能更远。快照无法区分，为 null。 */
  saturated: number | null
  /** 被不可达点截断的方向数（算路失败视为屏障）。 */
  barrier_truncated: number
  /** 首个采样点即不可达或超时、边界半径为 0 的方向数。 */
  zero_radius: number
}

/** 一次实时计算的配额消耗证据链。 */
export interface QuotaStats {
  /** 实发批量算路点对数。批量算路日配额按点对计量，这是最该盯的数。 */
  matrix_pairs: number
  /** 实发批量算路请求数（含失败重试）。 */
  matrix_requests: number
  /** 整块失败的矩阵请求数；相关网格按「未知」处理。 */
  matrix_failed_blocks: number
  /** 实发地点检索请求数。 */
  poi_queries: number
  /** 实发地理编码请求数。 */
  geocode_queries: number
  /** 实发坐标转换请求数（geoconv 无日配额限制）。 */
  geoconv_queries?: number
  /** 朴素做法点对数（网格 × 设施 × 品类）；null 表示未做盲区判定。 */
  naive_matrix_pairs: number | null
  /** 直线剪枝零请求完成判定的次数（网格 × 品类）。 */
  pruned_decisions: number | null
  /** 盲区判定网格数。 */
  grid_cells: number | null
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

/** 实时分析历史条目（轻量元数据）。 */
export interface HistoryMeta {
  id: number
  created_at: string
  lat: number
  lng: number
  minutes: number
  mode: string
  area_km2: number | null
  score: number | null
}

/** 一条完整的历史分析记录。 */
export interface HistoryRecord extends HistoryMeta {
  payload: Record<string, unknown>
  result: IsochroneFeature
}

/** 模拟新建：某一侧（前/后）的统计快照。 */
export interface SimulateStats {
  score: number | null
  grade: string | null
  blind_count: number | null
  blind_ratio: Record<string, number> | null
}

/** 模拟新建结果：在指定位置放设施后的前后对比（本地计算，零 API 消耗）。 */
export interface SimulationResult {
  category: string
  lat: number
  lng: number
  covered_cells: { lat: number; lng: number }[]
  covered_count: number
  before: SimulateStats
  after: SimulateStats
  approximation: string
}

export interface CategoryMeta {
  name: string
  /** 命题点名的三类设施（菜市场、药店、小学），盲区判定只对它们逐网格测距。 */
  key_facility: boolean
}

export interface TravelModeInfo {
  id: string
  label: string
  speed_m_per_s: number
  uses_traffic: boolean
  allow_grid_blindspots: boolean
  factors: string[]
}

export interface AppConfig {
  browser_ak: string
  default_minutes: number
  default_directions: number
  categories: CategoryMeta[]
  walk_limit_m: number
  modes: TravelModeInfo[]
  default_mode: string
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

export interface Place {
  category: string
  name: string
  lat: number
  lng: number
  in_circle: boolean
}

export interface Coverage {
  /** 等时圈**内**的设施数。 */
  categories: Record<string, number>
  /** 设施点，供地图打点。样例快照也带名称和坐标。 */
  places?: Place[]
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
  prescriptions?: Prescription[]
}

export interface Prescription {
  action: 'connect' | 'site' | 'densify' | 'network' | 'maintain'
  title: string
  reason: string
  category: string | null
  lat: number | null
  lng: number | null
  covers: number
}

export interface ApiErrorDetail {
  code: string
  message: string
  status?: number
}
