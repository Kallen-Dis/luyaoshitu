import type { AppConfig, ApiErrorDetail, HistoryMeta, HistoryRecord, IsochroneFeature, SampleMeta, SimulationResult } from './types'

/** 后端返回的结构化错误。保留 code 以便界面区分「配额耗尽」与「配置错误」等情形。 */
export class ApiError extends Error {
  readonly code: string
  readonly httpStatus: number

  constructor(detail: ApiErrorDetail, httpStatus: number) {
    super(detail.message)
    this.code = detail.code
    this.httpStatus = httpStatus
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
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

/** 模拟新建：在指定位置放一处设施，本地重算盲区与分数（零 API 消耗）。 */
export const simulate = (params: {
  category: string
  lat: number
  lng: number
  feature: IsochroneFeature
}) =>
  request<SimulationResult>('/api/simulate', {
    method: 'POST',
    body: JSON.stringify(params),
  })

export const computeIsochrone = (params: {
  lat: number
  lng: number
  minutes: number
  directions: number
  coverage?: boolean
  blindspots?: boolean
  mode?: string
  coord_sys?: string
}) =>
  request<IsochroneFeature>('/api/isochrone', {
    method: 'POST',
    body: JSON.stringify(params),
  })

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
  params: {
    lat: number
    lng: number
    minutes: number
    directions: number
    coverage?: boolean
    blindspots?: boolean
    mode?: string
    coord_sys?: string
  },
  handlers: IStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch('/api/isochrone/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
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
        { code: data.code ?? 'error', message: data.message ?? '分析失败' },
        data.status ?? 502,
      )
    } else if (event === 'done') {
      handlers.onDone?.(data.feature as IsochroneFeature)
    } else {
      handlers.onFeature?.(data.feature as IsochroneFeature, event)
    }
  }

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
