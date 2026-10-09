import { adminToken, deviceId, editToken } from './lib/markings'
import type {
  AdminMarkingDetail,
  AdminQueue,
  AppConfig,
  ApiErrorDetail,
  ClosureSpec,
  ConstructionResult,
  CrosscheckResult,
  HistoryMeta,
  HistoryRecord,
  IsochroneFeature,
  Marking,
  MarkingDetail,
  MarkingInput,
  MarkingPhoto,
  PhotoUpload,
  MarkingSpec,
  Narrative,
  RecheckResult,
  ReviewInput,
  SampleMeta,
  SimulationResult,
  SitePlanResult,
  TripOrigin,
  TripNearestResult,
  TripPlanResult,
  TripSelection,
  FreshFeedbackSummary,
  TripReanchorResult,
  TripNearbyResult,
  Place,
} from './types'

/** 实时分析请求参数，与后端 IsochroneRequest 一一对应。 */
export interface AnalysisParams {
  lat: number
  lng: number
  minutes: number
  directions: number
  coverage?: boolean
  blindspots?: boolean
  mode?: string
  coord_sys?: string
  /** 步行时用路线规划补回过街与路口等待 */
  crossing_delay?: boolean
  closures?: ClosureSpec[]
  /** 附近共享标注的使用方式：auto 已核实 + 自己的 + include；none 纯算法 */
  markings?: { mode: 'auto' | 'all' | 'none'; include: number[]; exclude: number[] }
  /** 显示名：搜索时输入的地址，写进结果；点地图、输坐标时不传 */
  name?: string
}

/** 后端返回的结构化错误。保留 code 以便界面区分「配额耗尽」与「配置错误」等情形。 */
export class ApiError extends Error {
  readonly code: string
  readonly httpStatus: number
  /** 后端给出的完整说明：哪个服务、停在哪一步、何时恢复 */
  readonly detail: ApiErrorDetail

  constructor(detail: ApiErrorDetail, httpStatus: number) {
    super(detail.message)
    this.code = detail.code
    this.httpStatus = httpStatus
    this.detail = detail
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // 设备标识随每个请求发给自己的后端：分析时据此认出「自己的标注」，标注接口据此限流与计票
  const resp = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      'X-Device-Id': deviceId(),
      ...(init?.headers ?? {}),
    },
  })
  if (!resp.ok) {
    let detail: ApiErrorDetail = { code: 'unknown', message: `请求失败（HTTP ${resp.status}）` }
    try {
      const body = await resp.json()
      if (body?.detail && typeof body.detail === 'object') detail = body.detail
      else if (typeof body?.detail === 'string') detail = { code: 'error', message: body.detail }
    } catch {
      // 响应体不是 JSON，沿用默认提示
    }
    throw new ApiError(detail, resp.status)
  }
  return resp.json() as Promise<T>
}

export const fetchConfig = () => request<AppConfig>('/api/config')

export const tripNearest = (params: {
  include_pending?: boolean; poi_query?: string
  feature: IsochroneFeature; origin: TripOrigin; category: string; limit?: number; target_place_id?: string
}, signal?: AbortSignal) => request<TripNearestResult>('/api/trip/nearest', { method: 'POST', body: JSON.stringify(params), signal })

export const tripPlan = (params: {
  feature: IsochroneFeature; origin: TripOrigin; stops: string[]; selected_stops?: TripSelection[]
  replace_stop?: TripSelection & { index: number }
  retry_routes?: boolean
  fixed_stops?: import('./types').TripFixedStop[]
}, signal?: AbortSignal) => request<TripPlanResult>('/api/trip/plan', { method: 'POST', body: JSON.stringify(params), signal })

/** 普通浏览不联网；显式 poi_query 只补检索门店，不计算路线。 */
export const tripOptions = (params: { feature: IsochroneFeature; origin: TripOrigin; categories: string[]; poi_query?: string }, signal?: AbortSignal) =>
  request<import('./types').TripOptionsResult>('/api/trip/options', { method: 'POST', body: JSON.stringify(params), signal })

export const tripReanchor = (params: {
  feature: IsochroneFeature; origin: TripOrigin; stops: string[]; selected_stops: TripSelection[]; places: Place[]
}, signal?: AbortSignal) => request<TripReanchorResult>('/api/trip/reanchor', { method: 'POST', body: JSON.stringify(params), signal })

export const tripGuideNearby = (params: { feature: IsochroneFeature; origin: TripOrigin; category: string }, signal?: AbortSignal) =>
  request<TripNearbyResult>('/api/trip/guide-nearby', { method: 'POST', body: JSON.stringify(params), signal })

export const fetchSamples = () =>
  request<{ samples: SampleMeta[] }>('/api/samples').then((r) => r.samples)

export const fetchSample = (id: string) => request<IsochroneFeature>(`/api/samples/${id}`)

export const fetchHistories = () =>
  request<{ items: HistoryMeta[] }>('/api/histories').then((r) => r.items)

export const fetchHistory = (id: number) =>
  request<HistoryRecord>(`/api/histories/${id}`)

export const geocode = (address: string) =>
  request<{ address: string; lat: number; lng: number }>(
    `/api/geocode?address=${encodeURIComponent(address)}`,
  )

/** 模拟新建：候选方格到拟建点做一次路网测距（通常不超过 100 个点对），重算盲区与分数。 */
export const simulateClosures = (feature: IsochroneFeature, closures: ClosureSpec[], signal?: AbortSignal) =>
  request<IsochroneFeature>('/api/simulate/closures', { method: 'POST', body: JSON.stringify({ feature, closures }), signal })

export const simulate = (params: {
  category: string
  lat: number
  lng: number
  feature: IsochroneFeature
  verify?: boolean
}, signal?: AbortSignal) =>
  request<SimulationResult>('/api/simulate', {
    method: 'POST',
    body: JSON.stringify(params),
    signal,
  })

/** 复测巡检：跳过缓存重取各方向步行路线（约 36 次路线规划，不占批量算路点对）。 */
export const recheck = (feature: IsochroneFeature) =>
  request<RecheckResult>('/api/recheck', {
    method: 'POST',
    body: JSON.stringify({ feature }),
  })

/** 工地 POI 候选：「工地」「施工」各检索一页，共 2 次地点检索，缓存 30 天。 */
export const constructionCandidates = (params: {
  lat: number
  lng: number
  radius_m?: number
  feature?: IsochroneFeature | null
}) =>
  request<ConstructionResult>('/api/construction-candidates', {
    method: 'POST',
    body: JSON.stringify(params),
  })

/** 核验选址：前几个备选点逐个路网核验（总点对受 budget_pairs 限制），按实测重排。 */
export const sitePlan = (params: {
  feature: IsochroneFeature
  category: string
  alternatives?: number
  budget_pairs?: number
}) =>
  request<SitePlanResult>('/api/site-plan', {
    method: 'POST',
    body: JSON.stringify(params),
  })

/** 综合结论：后端按模板由算法结果生成，零 API 消耗。 */
export const narrate = (feature: IsochroneFeature) =>
  request<Narrative>('/api/narrate', {
    method: 'POST',
    body: JSON.stringify({ feature }),
  })

/**
 * AI 二次核对：用百度地图 Agent Plan 的语义地点检索，再找一遍灰色区域附近的关键设施，
 * 列出疑似漏收录的设施。只给线索，不改结论；问过的问题后端直接读缓存。
 */
export const crosscheckFeature = (feature: IsochroneFeature, region?: string) =>
  request<CrosscheckResult>('/api/crosscheck', {
    method: 'POST',
    body: JSON.stringify({ feature, ...(region ? { region } : {}) }),
  })

// ---------- 共享标注 ----------

function withToken(markingId: number): Record<string, string> {
  const token = editToken(markingId)
  return token ? { 'X-Edit-Token': token } : {}
}

export const fetchMarkings = (lat: number, lng: number, radiusM = 2500) =>
  request<{ items: Marking[] }>(
    `/api/markings?lat=${lat.toFixed(6)}&lng=${lng.toFixed(6)}&radius_m=${Math.round(radiusM)}`,
  ).then((r) => r.items)

export const fetchMyMarkings = () =>
  request<{ items: Marking[] }>('/api/markings/mine').then((r) => r.items)

export const fetchMarking = (id: number) => request<MarkingDetail>(`/api/markings/${id}`)

export const createMarking = (input: MarkingInput) =>
  request<{ marking: Marking; edit_token: string }>('/api/markings', {
    method: 'POST',
    body: JSON.stringify(input),
  })

export type MarkingPatch = Partial<
  Pick<MarkingSpec, 'kind' | 'radius_m' | 'category' | 'categories' | 'name' | 'reason' | 'note'>
> & { expires_in_days?: number }

export const updateMarking = (id: number, version: number, patch: MarkingPatch) =>
  request<Marking>(`/api/markings/${id}`, {
    method: 'PATCH',
    headers: withToken(id),
    body: JSON.stringify({ ...patch, version }),
  })

export const retractMarking = (id: number) =>
  request<Marking>(`/api/markings/${id}`, { method: 'DELETE', headers: withToken(id) })

export const restoreMarking = (id: number) =>
  request<Marking>(`/api/markings/${id}/restore`, { method: 'POST', headers: withToken(id) })

export const revertMarking = (id: number, version: number, toVersion: number) =>
  request<Marking>(`/api/markings/${id}/revert`, {
    method: 'POST',
    headers: withToken(id),
    body: JSON.stringify({ version, to_version: toVersion }),
  })

export const voteMarking = (id: number, vote: -1 | 0 | 1) =>
  request<Marking>(`/api/markings/${id}/vote`, { method: 'PUT', body: JSON.stringify({ vote }) })

type FeedbackPlace = Pick<Place, 'name' | 'category' | 'lat' | 'lng'>
export const fetchFreshFeedback = (places: FeedbackPlace[], signal?: AbortSignal) =>
  request<{ items: FreshFeedbackSummary[] }>('/api/trip/fresh-feedback/summary', { method: 'POST', body: JSON.stringify({ places }), signal })
export const submitFreshFeedback = (place: FeedbackPlace, vote: -1 | 0 | 1, signal?: AbortSignal) =>
  request<FreshFeedbackSummary>('/api/trip/fresh-feedback', { method: 'PUT', body: JSON.stringify({ place, vote }), signal })

export async function deleteMarkingPhoto(photoId: string, markingId: number): Promise<void> {
  const resp = await fetch(`/api/markings/photos/${photoId}`, {
    method: 'DELETE',
    headers: { 'X-Device-Id': deviceId(), ...withToken(markingId) },
  })
  if (!resp.ok) throw await errorOf(resp)
}

async function errorOf(resp: Response): Promise<ApiError> {
  let detail: ApiErrorDetail = { code: 'unknown', message: `请求失败（HTTP ${resp.status}）` }
  try {
    const body = await resp.json()
    if (body?.detail && typeof body.detail === 'object') detail = body.detail
  } catch {
    // 响应体不是 JSON，沿用默认提示
  }
  return new ApiError(detail, resp.status)
}

/** 给已有的标注补一张照片。 */
export function uploadMarkingPhoto(
  markingId: number,
  blob: Blob,
  onProgress?: (ratio: number) => void,
): Promise<MarkingPhoto> {
  const token = editToken(markingId)
  return sendImage<MarkingPhoto>(
    `/api/markings/${markingId}/photos`,
    blob,
    token ? { 'X-Edit-Token': token } : {},
    onProgress,
  )
}

/**
 * 新建标注前先传现场照片（必填），拿到预传 ID，新建时一起提交。
 * 服务端在同一个事务里把照片挂上标注，不会出现「标注建好了、照片没传上」的半截状态。
 */
export function stageMarkingPhoto(
  blob: Blob,
  onProgress?: (ratio: number) => void,
): Promise<PhotoUpload> {
  return sendImage<PhotoUpload>('/api/markings/uploads', blob, {}, onProgress)
}

/**
 * 上传一张图（请求体就是图片字节）。用 XHR 而不是 fetch：fetch 拿不到上传进度，
 * 乡镇网络下传一张图可能要好几秒，没有进度条用户会以为卡住了。
 */
function sendImage<T>(
  url: string,
  blob: Blob,
  headers: Record<string, string>,
  onProgress?: (ratio: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)
    xhr.setRequestHeader('Content-Type', blob.type || 'image/jpeg')
    xhr.setRequestHeader('X-Device-Id', deviceId())
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(e.loaded / e.total)
    }
    xhr.onload = () => {
      let body: unknown = null
      try {
        body = JSON.parse(xhr.responseText)
      } catch {
        // 非 JSON 响应（比如代理返回的 413 页面）
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T)
        return
      }
      const detail =
        body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'object'
          ? (body.detail as ApiErrorDetail)
          : {
              code: 'upload_failed',
              message:
                xhr.status === 413 ? '图片太大，请压缩后再上传' : `照片上传失败（HTTP ${xhr.status}）`,
            }
      reject(new ApiError(detail, xhr.status))
    }
    xhr.onerror = () =>
      reject(new ApiError({ code: 'network', message: '网络中断，照片没有传上去' }, 0))
    xhr.send(blob)
  })
}

// ---------- 管理员审核 ----------

function adminHeaders(): Record<string, string> {
  const token = adminToken()
  return token ? { 'X-Admin-Token': token } : {}
}

export const fetchAdminQueue = (status: string) =>
  request<AdminQueue>(`/api/admin/markings?status=${encodeURIComponent(status)}`, {
    headers: adminHeaders(),
  })

export const fetchAdminMarking = (id: number) =>
  request<AdminMarkingDetail>(`/api/admin/markings/${id}`, { headers: adminHeaders() })

export const reviewMarking = (id: number, input: ReviewInput) =>
  request<AdminMarkingDetail>(`/api/admin/markings/${id}/review`, {
    method: 'POST',
    headers: adminHeaders(),
    body: JSON.stringify(input),
  })

export const setPhotoVisibility = (photoId: string, hidden: boolean, note: string) =>
  request<MarkingPhoto>(`/api/admin/photos/${photoId}/visibility`, {
    method: 'POST',
    headers: adminHeaders(),
    body: JSON.stringify({ hidden, note }),
  })

/** 管理员看照片（含已隐藏的）：要带口令，所以取成 blob 再给 <img>。 */
export async function fetchAdminPhoto(photoId: string): Promise<string> {
  const resp = await fetch(`/api/admin/photos/${photoId}`, {
    headers: { 'X-Device-Id': deviceId(), ...adminHeaders() },
  })
  if (!resp.ok) throw await errorOf(resp)
  return URL.createObjectURL(await resp.blob())
}

export interface IStreamHandlers {
  /** 阶段提示：计算进入下一段时调用（用于替换遮罩文案）。 */
  onStage?: (stage: string, message: string) => void
  /** 中间结果：等时圈 / 设施覆盖 / 盲区各自的渐进快照，可立即上屏。 */
  onFeature?: (feature: IsochroneFeature, stage: string) => void
  /** 完整结果（含报告、配额与历史号）。 */
  onDone?: (feature: IsochroneFeature) => void
}

/**
 * SSE 渐进式计算。完整分析可达几十秒，分段推送让等时圈先上图，
 * 而不是让用户对着遮罩等全程。错误经 error 事件抛出为 ApiError。
 */
export async function computeIsochroneStream(
  params: AnalysisParams,
  handlers: IStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch('/api/isochrone/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Device-Id': deviceId() },
    body: JSON.stringify(params),
    signal,
  })
  if (!resp.ok || !resp.body) {
    let detail: ApiErrorDetail = { code: 'unknown', message: `请求失败（HTTP ${resp.status}）` }
    try {
      const body = await resp.json()
      if (body?.detail && typeof body.detail === 'object') detail = body.detail
    } catch {
      // 响应体不是 JSON，沿用默认提示
    }
    throw new ApiError(detail, resp.status)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let finished = false

  const dispatch = (block: string) => {
    const eventMatch = block.match(/^event: (.+)$/m)
    const dataMatch = block.match(/^data: (.+)$/m)
    if (!eventMatch || !dataMatch) return
    const event = eventMatch[1].trim()
    const data = JSON.parse(dataMatch[1])
    if (event === 'stage') {
      handlers.onStage?.(data.stage, data.message)
    } else if (event === 'error') {
      throw new ApiError(
        { ...data, code: data.code ?? 'error', message: data.message ?? '分析失败' },
        data.status ?? 502,
      )
    } else if (event === 'done') {
      finished = true
      handlers.onDone?.(data.feature as IsochroneFeature)
    } else {
      handlers.onFeature?.(data.feature as IsochroneFeature, event)
    }
  }

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx = buffer.indexOf('\n\n')
      while (idx >= 0) {
        const block = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        if (block.trim()) dispatch(block)
        idx = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) dispatch(buffer)
  } catch (err) {
    // 出错时主动断开，别让后端继续算一个没人要的结果
    reader.cancel().catch(() => {})
    throw err
  }
  if (!finished) {
    // 连接断开却没收到 done：多半是代理超时或后端进程退出，不能让界面停在半截结果上装作完成
    throw new ApiError(
      { code: 'stream_interrupted', message: '分析连接中断，未收到完整结果。已画出的部分仅供参考。' },
      502,
    )
  }
}

/**
 * 导出任意一次结果（实时计算、历史记录、样例）。后端按当前口径重算报告后打包，零 API 消耗。
 * 返回文件内容与后端给的文件名，由调用方触发下载。
 */
export async function exportFeature(
  feature: IsochroneFeature,
  format: 'zip' | 'md' | 'geojson' | 'csv' | 'json',
): Promise<{ blob: Blob; filename: string }> {
  const resp = await fetch('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ feature, format }),
  })
  if (!resp.ok) {
    let detail: ApiErrorDetail = { code: 'unknown', message: `导出失败（HTTP ${resp.status}）` }
    try {
      const body = await resp.json()
      if (body?.detail && typeof body.detail === 'object') detail = body.detail
    } catch {
      // 响应体不是 JSON，沿用默认提示
    }
    throw new ApiError(detail, resp.status)
  }
  const header = resp.headers.get('Content-Disposition') ?? ''
  const match = header.match(/filename\*=UTF-8''([^;]+)/)
  const filename = match ? decodeURIComponent(match[1]) : `路遥识途_体检报告.${format}`
  return { blob: await resp.blob(), filename }
}

/** 离线模拟分析：零 API 消耗，确定性伪随机，结果标注 simulated。 */
export const fetchDemo = (params: {
  lat: number
  lng: number
  minutes: number
  directions: number
  mode?: string
}) => {
  const qs = new URLSearchParams({
    lat: String(params.lat),
    lng: String(params.lng),
    minutes: String(params.minutes),
    directions: String(params.directions),
    mode: params.mode ?? 'walk',
  })
  return request<IsochroneFeature>(`/api/demo?${qs}`)
}
