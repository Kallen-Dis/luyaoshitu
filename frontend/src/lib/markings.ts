/**
 * 共享标注的前端工具：设备标识、编辑凭据、照片压缩、几何校验与展示用的元数据。
 *
 * 设备标识与编辑凭据都存在 localStorage：现阶段没有账号，清掉浏览器数据就不能再修改
 * 自己之前提交的标注（界面上写明了这一点）。管理员口令只存 sessionStorage，关掉标签页即失效。
 */

import type { MarkingStatus, MarkingType } from '../types'

const DEVICE_KEY = 'lyst.device.v1'
const TOKENS_KEY = 'lyst.tokens.v1'
const ADMIN_KEY = 'lyst.admin.v1'

// localStorage 在隐私模式或被禁用时会抛异常；退回内存，至少本次会话内可用
const memory = new Map<string, string>()

function readStore(storage: 'local' | 'session', key: string): string | null {
  try {
    return (storage === 'local' ? window.localStorage : window.sessionStorage).getItem(key)
  } catch {
    return memory.get(`${storage}:${key}`) ?? null
  }
}

function writeStore(storage: 'local' | 'session', key: string, value: string | null) {
  try {
    const s = storage === 'local' ? window.localStorage : window.sessionStorage
    if (value === null) s.removeItem(key)
    else s.setItem(key, value)
  } catch {
    if (value === null) memory.delete(`${storage}:${key}`)
    else memory.set(`${storage}:${key}`, value)
  }
}

function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // 非安全上下文（用局域网 IP 打开的 http 页面）没有 randomUUID，但有 getRandomValues
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** 本浏览器的设备标识。只发给自己的后端，后端只存加盐哈希。 */
export function deviceId(): string {
  let id = readStore('local', DEVICE_KEY)
  if (!id || !/^[A-Za-z0-9_-]{16,64}$/.test(id)) {
    id = randomId()
    writeStore('local', DEVICE_KEY, id)
  }
  return id
}

function readTokens(): Record<string, string> {
  try {
    const raw = readStore('local', TOKENS_KEY)
    const parsed = raw ? JSON.parse(raw) : {}
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

export function editToken(markingId: number): string | null {
  return readTokens()[String(markingId)] ?? null
}

export function saveEditToken(markingId: number, token: string) {
  const tokens = readTokens()
  tokens[String(markingId)] = token
  writeStore('local', TOKENS_KEY, JSON.stringify(tokens))
}

export function adminToken(): string | null {
  return readStore('session', ADMIN_KEY)
}

export function saveAdminToken(token: string | null) {
  writeStore('session', ADMIN_KEY, token)
}

// ---------- 展示元数据 ----------

export interface TypeMeta {
  label: string
  short: string
  hint: string
  color: string
  soft: string
  glyph: string
}

export const TYPE_META: Record<MarkingType, TypeMeta> = {
  closure: {
    label: '围挡 / 封路',
    short: '围挡',
    hint: '施工围挡、临时封路、封闭小区或走不通的路',
    color: '#c2410c',
    soft: '#fff1e6',
    glyph: '围',
  },
  facility_missing: {
    label: '设施失效',
    short: '失效',
    hint: '地图上有，但已关闭、不对外或位置不对',
    color: '#be123c',
    soft: '#fff1f2',
    glyph: '失',
  },
  facility_extra: {
    label: '补录设施',
    short: '补录',
    hint: '村卫生室、社区菜点等地图没收录的设施',
    color: '#0f766e',
    soft: '#ecfdf5',
    glyph: '补',
  },
  gray_area: {
    label: '灰色区域',
    short: '灰区',
    hint: '算法没识别出来、但确实缺设施的地方',
    color: '#475569',
    soft: '#f1f5f9',
    glyph: '灰',
  },
}

export const STATUS_META: Record<MarkingStatus, { label: string; tone: string }> = {
  pending: { label: '待核实', tone: 'pending' },
  verified: { label: '已核实', tone: 'verified' },
  rejected: { label: '已驳回', tone: 'rejected' },
  retracted: { label: '已撤回', tone: 'muted' },
  archived: { label: '已归档', tone: 'muted' },
  expired: { label: '已过期', tone: 'muted' },
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

export function formatDistance(m: number | null | undefined): string {
  if (m == null) return ''
  if (m < 1) return '就在这里'
  return m < 1000 ? `${Math.round(m)} 米` : `${(m / 1000).toFixed(1)} 公里`
}

export function daysFromNow(days: number): string {
  const d = new Date(Date.now() + days * 86_400_000)
  return d.toLocaleDateString('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
}

// ---------- 几何（与后端 models.check_polygon 同一套规则，提前给用户反馈） ----------

const M_PER_DEG = 111_320

export interface LatLng {
  lat: number
  lng: number
}

export function metersBetween(a: LatLng, b: LatLng): number {
  const dLat = (b.lat - a.lat) * M_PER_DEG
  const dLng = (b.lng - a.lng) * M_PER_DEG * Math.cos((((a.lat + b.lat) / 2) * Math.PI) / 180)
  return Math.hypot(dLat, dLng)
}

function planar(points: LatLng[]): [number, number][] {
  const o = points[0]
  const k = M_PER_DEG * Math.cos((o.lat * Math.PI) / 180)
  return points.map((p) => [(p.lng - o.lng) * k, (p.lat - o.lat) * M_PER_DEG])
}

export function polygonAreaM2(points: LatLng[]): number {
  if (points.length < 3) return 0
  const xy = planar(points)
  let s = 0
  for (let i = 0; i < xy.length; i++) {
    const [x1, y1] = xy[i]
    const [x2, y2] = xy[(i + 1) % xy.length]
    s += x1 * y2 - x2 * y1
  }
  return Math.abs(s) / 2
}

function cross(o: [number, number], a: [number, number], b: [number, number]) {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
}

function segmentsCross(
  p1: [number, number],
  p2: [number, number],
  p3: [number, number],
  p4: [number, number],
) {
  const d1 = cross(p3, p4, p1)
  const d2 = cross(p3, p4, p2)
  const d3 = cross(p1, p2, p3)
  const d4 = cross(p1, p2, p4)
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))
}

/** 多边形草图的问题：返回给用户看的提示；没问题返回 null。 */
export function polygonProblem(points: LatLng[], maxVertices = 50, maxAreaKm2 = 1): string | null {
  if (points.length < 3) return `至少需要 3 个顶点，还差 ${3 - points.length} 个`
  if (points.length > maxVertices) return `最多 ${maxVertices} 个顶点`
  const xy = planar(points)
  const n = xy.length
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue
      if (segmentsCross(xy[i], xy[(i + 1) % n], xy[j], xy[(j + 1) % n])) {
        return '轮廓自相交了：请按顺序沿边界点，或撤销最后几个点'
      }
    }
  }
  const area = polygonAreaM2(points)
  if (area < 400) return '区域太小，请画大一些'
  if (area > maxAreaKm2 * 1e6) return `超过 ${maxAreaKm2} 平方公里，请拆成几块分别标注`
  return null
}

/** 圆与折线是否相交：用来在提交围挡前预告它会挡住几个方向的步行路线。 */
export function circleHitsPath(center: LatLng, radiusM: number, path: [number, number][]): boolean {
  const k = M_PER_DEG * Math.cos((center.lat * Math.PI) / 180)
  const xy = path.map(([lng, lat]) => [(lng - center.lng) * k, (lat - center.lat) * M_PER_DEG])
  for (let i = 0; i + 1 < xy.length; i++) {
    const [ax, ay] = xy[i]
    const [bx, by] = xy[i + 1]
    const dx = bx - ax
    const dy = by - ay
    const len2 = dx * dx + dy * dy
    const t = len2 === 0 ? 0 : Math.max(0, Math.min(1, -(ax * dx + ay * dy) / len2))
    if (Math.hypot(ax + t * dx, ay + t * dy) <= radiusM) return true
  }
  return false
}

// ---------- 照片 ----------

export interface PreparedPhoto {
  blob: Blob
  url: string
  width: number
  height: number
  name: string
}

const MAX_SIDE = 1600

/**
 * 上传前在浏览器里重新编码成 JPEG：长边缩到 1600 像素以内，同时丢掉 EXIF
 * （手机照片里通常带着拍摄地点）。服务端还会再清一遍，这一步主要是省流量。
 */
export async function preparePhoto(file: File, maxBytes: number): Promise<PreparedPhoto> {
  if (!file.type.startsWith('image/') && !/\.(jpe?g|png|webp|heic|heif)$/i.test(file.name)) {
    throw new Error(`「${file.name}」不是图片`)
  }
  let bitmap: ImageBitmap
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    throw new Error(
      `读不了「${file.name}」。如果是 iPhone 的 HEIC 照片，请在相机设置里改为「兼容性最佳」或先转成 JPG`,
    )
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height))
  const width = Math.max(1, Math.round(bitmap.width * scale))
  const height = Math.max(1, Math.round(bitmap.height * scale))
  if (Math.min(width, height) < 16) {
    bitmap.close()
    throw new Error(`「${file.name}」太小了，看不清现场`)
  }
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    bitmap.close()
    throw new Error('浏览器不支持图片处理')
  }
  ctx.drawImage(bitmap, 0, 0, width, height)
  bitmap.close()
  let blob: Blob | null = null
  for (const quality of [0.85, 0.72, 0.6]) {
    blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality))
    if (blob && blob.size <= maxBytes) break
  }
  if (!blob || blob.size > maxBytes) throw new Error(`「${file.name}」压缩后仍然太大`)
  return { blob, url: URL.createObjectURL(blob), width, height, name: file.name }
}
