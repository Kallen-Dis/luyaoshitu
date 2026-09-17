import type { AppConfig, ApiErrorDetail, IsochroneFeature, SampleMeta } from './types'

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

export const geocode = (address: string) =>
  request<{ address: string; lat: number; lng: number }>(
    `/api/geocode?address=${encodeURIComponent(address)}`,
  )

export const computeIsochrone = (params: {
  lat: number
  lng: number
  minutes: number
  directions: number
  coverage?: boolean
  blindspots?: boolean
  mode?: string
}) =>
  request<IsochroneFeature>('/api/isochrone', {
    method: 'POST',
    body: JSON.stringify(params),
  })
