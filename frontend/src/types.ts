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
  /** 过街与路口等待校正的汇总（仅步行、且做了路线校正时存在）。 */
  delay?: DelayInfo
  /** 未计过街等待、未按围挡截断的外圈，[经度, 纬度]。 */
  raw_ring?: [number, number][]
  /** 本次计算使用的施工围挡。 */
  closures?: ClosureSpec[]
  /** 非步行方式时，说明过街校正与围挡未生效。 */
  refine_skipped?: string
  history_id?: number
  /** 没有中断、但做了降级的环节（某品类检索失败、方格没测到、路况用了旧数据等）。 */
  warnings?: AnalysisWarning[]
  /** 驾车实时路况：本次用到的路况有多旧。 */
  traffic?: TrafficInfo | null
  /** 这次计算叠加了哪些用户标注、与纯算法结果差多少 */
  markings?: MarkingsResult
}

export interface AnalysisWarning {
  stage: string
  stage_label: string
  message: string
}

export interface TrafficInfo {
  /** 多少分钟内的路况缓存视为实时 */
  fresh_ttl_min: number
  /** 本次用到的路况中最旧的一条是多少分钟前查到的 */
  max_age_min: number
  /** 接口失败、改用过期路况兜底的采样点数 */
  stale_points: number
  stale_max_age_min?: number
}

/** 用户在地图上标注的施工围挡，以圆近似。 */
export interface ClosureSpec {
  lat: number
  lng: number
  radius_m: number
  label?: string
}

/** 过街等待校正：每个方向一条步行路线，把步骤里超出匀速步行的秒数补回耗时。 */
export interface DelayInfo {
  applied: boolean
  source: string
  base_speed_m_per_s: number
  routes_requested: number
  routes_ok: number
  delay_per_km_s: number | null
  boundary_crossings: number
  raw_area_km2: number
  area_km2: number
  area_ratio: number | null
  closures: ClosureSpec[]
  closure_rays: number
  note: string
}

/** 等时圈质量诊断：让用户知道哪些方向是截断/饱和，而不是把多边形当确定结果。 */
export interface IsochroneQuality {
  directions: number
  /** 采样上界内始终未超时的方向数；真实边界可能更远。快照无法区分，为 null。 */
  saturated: number | null
  /** 被不可达点截断的方向数（算路失败视为屏障）。 */
  barrier_truncated: number
  /** 被用户标注的施工围挡截断的方向数。 */
  closure_truncated?: number
  /** 首个采样点即不可达或超时、边界半径为 0 的方向数。 */
  zero_radius: number
  /** 步行路线没取到、只能用批量算路耗时的方向数。 */
  route_failed?: number
}

/** 一次实时计算的配额消耗证据链。 */
export interface QuotaStats {
  /** 实发批量算路点对数。批量算路日配额按点对计量，这是最该盯的数。 */
  matrix_pairs: number
  /** 实发批量算路请求数（含失败重试）。 */
  matrix_requests: number
  /** 整块失败的矩阵请求数；相关网格按「未知」处理。 */
  matrix_failed_blocks: number
  /** 实发步行路线规划次数（过街校正与围挡核验）。 */
  route_requests?: number
  route_failed?: number
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

/** 模拟新建结果：候选方格到拟建点做路网测距后的前后对比。 */
export interface SimulationResult {
  category: string
  lat: number
  lng: number
  /** network：逐格实测步行距离；estimate / estimate_quota：只按直线估算的上限。 */
  basis: 'network' | 'estimate' | 'estimate_quota'
  covered_cells: { lat: number; lng: number }[]
  covered_count: number
  candidate_count: number
  /** 本次核验消耗的批量算路点对数。 */
  pairs_used: number
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
  server_ak_configured?: boolean
  /** 启动配置问题（缺服务端 / 浏览器端 AK），前端必须明确展示 */
  warnings?: { code: string; message: string }[]
  default_minutes: number
  default_directions: number
  categories: CategoryMeta[]
  walk_limit_m: number
  grid?: { spacing_m: number; extent_m: number }
  closure_limits?: { max_count: number; min_radius_m: number; max_radius_m: number }
  modes: TravelModeInfo[]
  default_mode: string
  /** AI 二次核对（百度地图 Agent Plan）能不能用；Token 不下发 */
  agent_plan?: { configured: boolean }
  /** 共享标注的枚举、文案与上限 */
  markings?: MarkingConfig
}

export interface RayMetric {
  bearing: number
  radius_m: number
  detour: number | null
  barrier: boolean
  /** 截断原因是施工围挡 */
  closure?: boolean
  /** 边界以内累计的过街等待（秒） */
  delay_s?: number
  crossings?: number
  route?: 'ok' | 'failed' | 'skipped'
  /** 复测巡检的基线：该方向步行路线的终点 [纬度, 经度]、路网距离与化简折线 [经度, 纬度] */
  route_to?: [number, number]
  route_m?: number
  route_path?: [number, number][]
  route_fetched_at?: string
}

/** 复测巡检里一个方向的比较结果。 */
export interface RecheckRay {
  bearing: number
  old_m: number
  new_m: number | null
  delta_m: number | null
  status: 'same' | 'longer' | 'shorter' | 'rerouted' | 'failed'
  old_path?: [number, number][]
  new_path?: [number, number][]
}

/** 复测发现的疑似新增阻断（相邻方向落在同一处的已合并）。 */
export interface RecheckSuspect {
  id: string
  label: string
  lat: number
  lng: number
  radius_m: number
  precise: boolean
  span_m: number
  /** 被放弃的旧路段 [经度, 纬度] */
  segment: [number, number][]
  bearings: number[]
  old_m: number
  new_m: number
  max_delta_m: number
  distance_m: number
  direction: string
  reason: string
}

export interface RecheckResult {
  checked: number
  same: number
  longer: number
  shorter: number
  rerouted: number
  failed: number
  rays: RecheckRay[]
  suspects: RecheckSuspect[]
  improved: { bearing: number; old_m: number; new_m: number }[]
  baseline: { fetched_at: string | null; age_days: number | null }
  checked_at: string
  route_requests: number
  route_failures: string[]
  note: string
}

/** 工地 POI 候选。 */
export interface ConstructionCandidate {
  name: string
  address: string
  lat: number
  lng: number
  keyword: string
  strength: 'strong' | 'weak'
  distance_m: number
  bearing: number
  near_route_m: number | null
  on_route: boolean
  on_route_bearing: number | null
  radius_m: number
}

export interface ConstructionResult {
  candidates: ConstructionCandidate[]
  keywords: string[]
  failed_keywords: string[]
  raw_count: number
  dropped: number
  radius_m: number
  routes_compared: number
  poi_queries: number
  note: string
}

/** 核验选址里的一个备选点。 */
export interface SitePlanCandidate {
  lat: number
  lng: number
  estimated: number
  estimated_in_circle: number
  region: string | null
  rank_estimate: number
  pairs_needed: number
  status: 'verified' | 'estimated' | 'over_budget'
  verified: number | null
  place?: { address: string; description: string; street: string } | null
}

export interface SitePlanResult {
  category: string
  basis: { demand_cells: number; detour: number; radius_m: number }
  candidates: SitePlanCandidate[]
  best: SitePlanCandidate & { simulation: SimulationResult }
  pairs_used: number
  budget_pairs: number
  verified: boolean
  note: string
  quota?: { matrix_pairs: number; regeo_queries: number }
}

/** 综合结论：后端按模板由算法结果生成的四段话。 */
export interface Narrative {
  text: string
  generated_at: string
}

/** AI 二次核对里 Agent Plan 返回的一处同类设施（坐标已换算成 BD09）。 */
export interface CrosscheckPlace {
  name: string
  lat: number
  lng: number
  /** 和结果里已收录的哪一家是同一处；null 表示没收录 */
  matched: string | null
  /** 离这片区域里缺这一类的最近方格的直线距离（米） */
  nearest_gap_m: number | null
  from_anchor_m: number
}

/** 一片灰色区域 × 一个缺的品类的核对结果。 */
export interface CrosscheckRow {
  region: string
  category: string
  cells: number
  supply_cells: number
  barrier_cells: number
  /** 这个问题没核对成功的原因；不是「没有设施」 */
  error: string | null
  returned: number
  found: CrosscheckPlace[]
  matched: number
  suspects: number
}

/** 疑似漏收录：没收录、且离缺口方格直线不到 1 公里。 */
export interface CrosscheckSuspect extends CrosscheckPlace {
  region: string
  category: string
}

export interface CrosscheckResult {
  /** 提问时带的城市与区县，如「上海市普陀区」 */
  region_name: string
  rows: CrosscheckRow[]
  suspects: CrosscheckSuspect[]
  /** 超出单次问题数上限、没问的「区域 × 品类」 */
  skipped: number
  /** Token 无效等致命错误时中途停下的原因 */
  aborted: string | null
  agent_plan: { requests: number; cache_hits: number }
  max_questions: number
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
  /** user：用户补录并参与了本次计算的设施 */
  source?: 'user'
  marking_id?: number
  /** 入口（校门、导航点）。有入口的设施按最近的入口测距，没有就按坐标点 */
  entries?: { lat: number; lng: number; name: string }[]
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
  /** 中心点步行到此的耗时（秒，已补过街等待），热力图强度由它决定。null 表示测距失败或受围挡阻断。 */
  reach_s: number | null
  /** 批量算路给的耗时（不含过街等待）。 */
  reach_raw_s?: number | null
  /** 品类 -> 最近设施的真实路网步行距离（米）。 */
  nearest_m: Record<string, number | null>
  /** 步行 1 公里内确无此类设施 */
  missing: string[]
  /** 测距失败，既不能判有也不能判无 */
  unknown: string[]
  /** 是否落在 15 分钟等时圈内。旧版快照没有此字段，视为在圈内。 */
  in_circle?: boolean
  /** 从中心过来的路线被施工围挡挡住 */
  closure_blocked?: boolean
}

export interface Blindspots {
  /** network：真实路网实测；simulated：离线模拟。旧版快照没有此字段。 */
  basis?: string
  /**
   * polygon：只在 15 分钟步行圈内布点（现版，灰色区域只标圈内）；
   * disc：中心 1.5 公里圆形网格（较早的结果，后端打开时已收成圈内，scoped_from 记为 disc）。
   */
  layout?: string
  grid_spacing_m: number
  extent_m?: number | null
  walk_limit_m: number
  cell_count: number
  blind_count: number
  in_circle_count?: number
  max_reach_s: number | null
  blind_ratio: Record<string, number>
  cells: GridCell[]
  delay_applied?: boolean
  /** 判定成功的方格不到一半、没有给出占比的品类 */
  incomplete_categories?: string[]
  closure_check?: {
    checked_pairs: number
    blocked_pairs: number
    unverified_pairs: number
    excluded_places: number
  } | null
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
  /** 直线画圆的面积：只用来说明直线法高估了几倍，不是评分基准 */
  ideal_area_km2: number
  /** 理想方格路网同样时间走得到的菱形面积：「路网可达」的满分基准。较早的结果没有 */
  grid_area_km2?: number
  area_ratio: number
  straight_inflation: number | null
  categories: Record<string, number> | null
  failed_categories: string[]
  blind_ratio: Record<string, number> | null
  blind_cell_count: number | null
  cell_count: number | null
  /** 落在 15 分钟圈内的盲区格数与总格数 */
  blind_in_circle?: number | null
  cells_in_circle?: number | null
  /** 网格判定的口径说明 */
  grid_basis?: string | null
  /** 自动标注的灰色区域（相邻盲区格合并成片）与逐片成因诊断 */
  gray_regions?: GrayRegions | null
  coverage_source: string | null
  coverage_pending: boolean
  blindspots_pending: boolean
  prescriptions?: Prescription[]
}

/** 一片灰色区域里某一品类的成因诊断。 */
export interface RegionDiagnosis {
  category: string
  cells: number
  /** 直线 1 公里内就有同类设施、步行却超过 1 公里：路网阻隔 */
  barrier_cells: number
  /** 直线 1 公里内都没有：供给缺口 */
  supply_cells: number
  cause: 'barrier' | 'supply' | 'unknown'
  nearby?: {
    straight_m: number
    walk_m: number | null
    place: string
    direction: string
  } | null
}

export interface GrayRegion {
  /** A、B、C……；超出编号上限的零散块为 null */
  id: string | null
  label: string
  cells: number
  area_km2: number
  in_circle_cells: number
  missing: Record<string, number>
  diagnosis: RegionDiagnosis[]
  anchor: { lat: number; lng: number }
  /** 外框，[经度, 纬度] 环 */
  rings: [number, number][][]
  summary: string
}

export interface GrayRegions {
  regions: GrayRegion[]
  region_count: number
  blind_cells: number
  diagnosable: boolean
  basis?: string
}

export interface Prescription {
  action: 'connect' | 'site' | 'densify' | 'network' | 'maintain'
  title: string
  reason: string
  category: string | null
  lat: number | null
  lng: number | null
  covers: number
  /** estimate：按直线距离 × 典型绕行估算，未经路网核验 */
  basis?: 'estimate'
  /** 打通：被同一家挡在外面的格数 */
  cells?: number
  region?: string | null
  direction?: string
  target?: { name: string; lat: number; lng: number }
  /** 补设：同一品类的第几处 */
  rank?: number
}

export interface ApiErrorDetail {
  code: string
  message: string
  status?: number
  /** 出错的百度服务，如「批量算路（步行）」 */
  service?: string
  /** 停在哪一步，如「等时圈采样」 */
  stage_label?: string | null
  /** 配额重置时间（仅当日配额超限） */
  reset?: string | null
  /** 新建标注撞上附近已有的同类标注时，后端带回那一条 */
  existing?: Marking
  /** 校验失败的字段 */
  field?: string | null
}

// ---------- 用户共享标注 ----------

export type MarkingType = 'closure' | 'facility_missing' | 'facility_extra' | 'gray_area'
export type MarkingStatus =
  | 'pending'
  | 'verified'
  | 'rejected'
  | 'retracted'
  | 'archived'
  | 'expired'

/** 标注的权威内容（与后端 spec 一一对应）。多边形为 [经度, 纬度]。 */
export interface MarkingSpec {
  type: MarkingType
  note: string
  kind?: string
  lat?: number
  lng?: number
  radius_m?: number
  category?: string
  name?: string
  reason?: string
  polygon?: [number, number][]
  categories?: string[]
}

export interface MarkingReview {
  decision: 'verify' | 'reject' | 'archive' | 'reopen'
  basis: string[]
  reason: string | null
  note: string
  at: string
}

export interface Marking {
  id: number
  type: MarkingType
  title: string
  status: MarkingStatus
  spec: MarkingSpec
  lat: number
  lng: number
  source: 'user' | 'recheck' | 'poi' | 'agent_plan'
  version: number
  confirms: number
  disputes: number
  /** 异议比确认多 2 票以上 */
  disputed: boolean
  confidence: number
  my_vote: -1 | 0 | 1
  photo_count: number
  mine: boolean
  created_at: string
  updated_at: string
  expires_at: string
  review: MarkingReview | null
  distance_m?: number
}

export interface MarkingPhoto {
  id: string
  url: string
  width: number
  height: number
  role: 'author' | 'witness'
  status: 'visible' | 'hidden'
  mine: boolean
  created_at: string
}

export interface MarkingEvent {
  action: string
  label: string
  actor: 'author' | 'other' | 'admin' | 'system'
  detail: string
  version: number | null
  at: string
}

export interface MarkingVersion {
  version: number
  title: string
  at: string
  spec?: MarkingSpec
}

export interface MarkingDetail extends Marking {
  photos: MarkingPhoto[]
  events: MarkingEvent[]
  versions: MarkingVersion[]
}

export interface AdminMarkingDetail extends MarkingDetail {
  author_history: { total: number; verified: number; rejected: number }
  self_submitted: boolean
  review_ticket: { opened_at: string; min_seconds: number }
}

export interface AdminQueue {
  queue: string
  items: Marking[]
  counts: Record<string, number>
}

export interface MarkingConfig {
  types: Record<MarkingType, string>
  closure_kinds: Record<string, { label: string; ttl_days: number }>
  missing_reasons: Record<string, string>
  gray_reasons: Record<string, string>
  type_ttl_days: Record<string, number>
  sources: Record<string, string>
  categories: string[]
  limits: {
    note_max: number
    name_max: number
    closure_radius_m: [number, number]
    polygon_max_vertices: number
    polygon_max_area_km2: number
    ttl_days: [number, number]
  }
  status_labels: Record<string, string>
  review_basis: Record<string, string>
  reject_reasons: Record<string, string>
  photo: {
    /** 现场照片必填：新建时至少附 min_per_marking 张 */
    required: boolean
    min_per_marking: number
    max_bytes: number
    max_per_marking: number
    max_per_uploader: number
    /** 预传的照片多久内要提交，过期作废 */
    upload_ttl_s: number
    formats: string[]
  }
  review: { min_seconds: number; note_min: number }
  admin_enabled: boolean
}

/** 结果里记下的一条标注（含版本，结果可追溯）。 */
export interface MarkingSummary {
  id: number
  version: number
  type: MarkingType
  title: string
  status: MarkingStatus
  mine: boolean
  source: string
  confidence: number
  disputed: boolean
  confirms: number
  disputes: number
  photo_count: number
  distance_m: number | null
  spec: MarkingSpec
  /** 已使用的标注：它对结果的影响 */
  effect?: string
  /** 未使用的标注：原因 */
  reason?: string
}

export interface MarkingsResult {
  mode: 'auto' | 'all' | 'none'
  query_radius_m: number
  nearby_count: number
  applied: MarkingSummary[]
  suggested: MarkingSummary[]
  skipped: MarkingSummary[]
  baseline: {
    total: number
    grade: string
    area_km2: number
    blind_cells: number | null
    blind_in_circle: number | null
    gray_regions: number | null
    ring: [number, number][] | null
    /**
     * 在地图上还原纯算法结果用的差异（较早保存的结果没有这些字段）：
     * 与叠加后不同的格子（完整的纯算法格）、设施点与灰色区域，以及围挡改了圈时的内圈与外圈。
     */
    inner_rings?: { minutes: number; coordinates: [number, number][] }[] | null
    raw_ring?: [number, number][] | null
    cells_changed?: GridCell[]
    places?: Place[] | null
    regions?: GrayRegions | null
  } | null
  effect: {
    score_algorithm: number
    score_with_markings: number
    score_delta: number | null
    blind_cells_delta: number | null
    area_delta_km2: number | null
    gray_regions_delta: number | null
  } | null
}

/** 新建标注的请求体。 */
export interface MarkingInput {
  type: MarkingType
  kind?: string
  lat?: number
  lng?: number
  radius_m?: number
  polygon?: [number, number][]
  category?: string
  categories?: string[]
  name?: string
  reason?: string
  note?: string
  expires_in_days?: number
  source?: 'user' | 'recheck' | 'poi' | 'agent_plan'
  force?: boolean
  /** 预传照片的 ID（必填，至少一张） */
  photos: string[]
}

/** 新建标注前预传的一张照片。 */
export interface PhotoUpload {
  id: string
  width: number
  height: number
  expires_at: string
}

export interface ReviewInput {
  decision: 'verify' | 'reject' | 'archive' | 'reopen'
  version: number
  note: string
  basis?: string[]
  checks?: { location: boolean; type: boolean; current: boolean }
  photos_reviewed?: string[]
  reason?: string
  expires_in_days?: number
}
