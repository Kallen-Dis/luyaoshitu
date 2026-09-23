import { useEffect, useRef, useState } from 'react'
import { loadBaiduMap } from '../baiduMap'
import { dissolveCells } from '../lib/dissolve'
import { HEAT_COLORS, buildHeatTile } from '../lib/heatRaster'
import type { GridCell, IsochroneFeature, Prescription } from '../types'

interface Props {
  ak: string
  center: { lat: number; lng: number }
  isochrone: IsochroneFeature | null
  showHeatmap: boolean
  showBlindspots: boolean
  /** 'all' 表示「缺任一关键设施」，否则为某个品类名。 */
  blindCategory: string
  onPickCenter: (lat: number, lng: number) => void
  onError: (message: string) => void
  /** 关闭时点击地图不改中心、不算路，避免演示误触烧配额。 */
  pickEnabled: boolean
}

/** 盲区按品类分色。同时看三类会糊成一片，故界面上一次只画一层。 */
const BLIND_COLORS: Record<string, string> = {
  all: '#dc2626',
  生鲜采买: '#ea580c',
  医药: '#dc2626',
  基础教育: '#7c3aed',
}

const PLAN_COLORS: Record<string, string> = {
  connect: '#1f6feb',
  site: '#9a6700',
  densify: '#8250df',
  network: '#0969da',
  maintain: '#1a7f37',
}

const ACTION_SHORT: Record<string, string> = {
  connect: '打通',
  site: '补设',
  densify: '加密',
  network: '路网',
  maintain: '维持',
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

/** 把网格点扩成正方形色块。用于标出测距失败的网格。 */
function cellCorners(cell: GridCell, spacingM: number) {
  const half = spacingM / 2
  const dLat = half / 111_320
  const dLng = half / (111_320 * Math.cos((cell.lat * Math.PI) / 180))
  return [
    [cell.lng - dLng, cell.lat - dLat],
    [cell.lng + dLng, cell.lat - dLat],
    [cell.lng + dLng, cell.lat + dLat],
    [cell.lng - dLng, cell.lat + dLat],
  ] as const
}

function cellsMissing(cells: GridCell[], category: string): GridCell[] {
  return category === 'all'
    ? cells.filter((c) => c.missing.length > 0)
    : cells.filter((c) => c.missing.includes(category))
}

/**
 * 地图视图：渲染等时圈、步行耗时热力图、服务盲区与规划建议，并支持点击改选中心点。
 *
 * 百度地图实例通过 ref 持有而非放进 state——它是命令式的可变对象，
 * 放进 state 会触发无意义的重渲染，还可能导致地图被反复销毁重建。
 */
export function MapView({
  ak,
  center,
  isochrone,
  showHeatmap,
  showBlindspots,
  blindCategory,
  onPickCenter,
  onError,
  pickEnabled,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const heatRef = useRef<HTMLCanvasElement>(null)
  const mapRef = useRef<any>(null)
  const overlaysRef = useRef<any[]>([])
  // 地图实例是异步建好的，而快照往往先到：只用 ref 持有实例的话，
  // 绘制 effect 会在实例就绪前空跑一次，此后再无触发，首屏就成了一张白底底图。
  const [ready, setReady] = useState(false)
  // 点击回调里要用到最新的处理函数，但地图监听只注册一次，故用 ref 转发
  const pickRef = useRef(onPickCenter)
  pickRef.current = onPickCenter
  // 点击盲区或规划圆点只应弹出详情。若任其冒泡到地图，就会被当成「改选中心点」
  // 而触发一次完整的实时计算——白烧配额，且用户根本没打算换点。
  const suppressPickRef = useRef(false)
  const pickEnabledRef = useRef(pickEnabled)
  pickEnabledRef.current = pickEnabled

  useEffect(() => {
    let cancelled = false

    loadBaiduMap(ak)
      .then(() => {
        if (cancelled || !containerRef.current || mapRef.current) return
        const BMapGL = window.BMapGL
        const map = new BMapGL.Map(containerRef.current)
        map.centerAndZoom(new BMapGL.Point(center.lng, center.lat), 15)
        map.enableScrollWheelZoom(true)
        map.addControl(new BMapGL.ScaleControl())
        map.addControl(new BMapGL.ZoomControl())
        map.addEventListener('click', (e: any) => {
          if (suppressPickRef.current) {
            suppressPickRef.current = false
            return
          }
          if (!pickEnabledRef.current) return
          if (e.latlng) pickRef.current(e.latlng.lat, e.latlng.lng)
        })
        mapRef.current = map
        setReady(true)
      })
      .catch((err: Error) => {
        if (!cancelled) onError(err.message)
      })

    return () => {
      cancelled = true
    }
    // 仅在 AK 变化时重建地图；中心点变化由下面的 effect 单独处理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ak])

  // 绘制热力图、盲区轮廓、等时圈与规划建议
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL) return

    overlaysRef.current.forEach((o) => map.removeOverlay(o))
    overlaysRef.current = []

    const add = (overlay: any) => {
      map.addOverlay(overlay)
      overlaysRef.current.push(overlay)
    }

    const blindspots = isochrone?.properties.blindspots
    const spacing = blindspots?.grid_spacing_m ?? 150

    // 测距失败的网格单独标出来：热力层里它们没有值，不能让人误读成「很近」
    if (blindspots && showHeatmap) {
      for (const cell of blindspots.cells) {
        if (cell.reach_s !== null) continue
        const corners = cellCorners(cell, spacing).map(
          ([lng, lat]) => new BMapGL.Point(lng, lat),
        )
        add(
          new BMapGL.Polygon(corners, {
            strokeWeight: 0,
            fillColor: '#9ca3af',
            fillOpacity: 0.45,
          }),
        )
      }
    }

    if (blindspots && showBlindspots) {
      const blindCells = cellsMissing(blindspots.cells, blindCategory)
      const color = BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all
      // 相邻盲区格子并成连片轮廓。画的仍是被判定格子的并集，只是不再是棋盘格
      for (const ring of dissolveCells(blindCells, spacing)) {
        const points = ring.path.map((p) => new BMapGL.Point(p.lng, p.lat))
        const area = new BMapGL.Polygon(points, {
          strokeColor: color,
          strokeWeight: ring.hole ? 2 : 3.5,
          strokeOpacity: 1,
          strokeStyle: ring.hole ? 'dashed' : 'solid',
          fillColor: color,
          // 空洞是被盲区围住的达标区域，填上就等于把好的一片说成坏的
          fillOpacity: ring.hole ? 0 : 0.34,
        })
        area.addEventListener('click', (e: any) => {
          suppressPickRef.current = true
          const hit = nearestCell(blindCells, e.latlng?.lat, e.latlng?.lng)
          if (!hit) return
          map.openInfoWindow(
            new BMapGL.InfoWindow(describeCell(hit, blindspots.walk_limit_m), {
              width: 250,
              title: blindCategory === 'all' ? '服务盲区' : `服务盲区·${blindCategory}`,
            }),
            new BMapGL.Point(hit.lng, hit.lat),
          )
        })
        add(area)
      }
    }

    // 等时圈轮廓画在网格之上：盲区连成片时，压在下面的边界会被整片红色吃掉
    if (isochrone) {
      const ring = isochrone.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      add(
        new BMapGL.Polygon(points, {
          strokeColor: '#0b3f9e',
          strokeWeight: 3,
          strokeOpacity: 1,
          fillColor: '#1f6feb',
          // 叠了热力层后填充会盖住色阶，此时只保留轮廓
          fillOpacity: blindspots && showHeatmap ? 0 : 0.18,
        }),
      )
      // 视野贴合等时圈范围，比固定缩放级别更实用——
      // 不同社区的可达范围差异很大，桃浦镇比曹杨新村小了近三成
      map.setViewport(points)
    } else {
      map.setCenter(new BMapGL.Point(center.lng, center.lat))
    }

    add(new BMapGL.Marker(new BMapGL.Point(center.lng, center.lat)))

    const prescriptions = (isochrone?.properties.report?.prescriptions ?? []).filter(
      (p: Prescription) => p.lat != null && p.lng != null,
    )
    // 多条处方常落在同一个盲区质心附近（桃浦三类设施都指向同一片）。
    // 标签按簇堆叠在同一锚点上，否则互相压着谁都看不清；圆圈仍留在各自坐标。
    const anchors = labelAnchors(prescriptions)
    prescriptions.forEach((p, i) => {
      const point = new BMapGL.Point(p.lng, p.lat)
      const anchor = anchors[i]
      const color = PLAN_COLORS[p.action] ?? '#1f6feb'
      const ring = new BMapGL.Circle(point, 110, {
        strokeColor: color,
        strokeWeight: 2,
        strokeOpacity: 0.95,
        fillColor: color,
        fillOpacity: 0.22,
      })
      const text = p.category
        ? `${ACTION_SHORT[p.action] ?? p.action}·${p.category}`
        : (ACTION_SHORT[p.action] ?? p.action)
      const label = new BMapGL.Label(text, {
        position: new BMapGL.Point(anchor.lng, anchor.lat),
        offset: new BMapGL.Size(-30, -12 + anchor.slot * 24),
      })
      label.setStyle({
        color: '#fff',
        background: color,
        border: '1px solid rgba(255, 255, 255, 0.85)',
        borderRadius: '10px',
        padding: '2px 7px',
        fontSize: '11px',
        fontWeight: '600',
        boxShadow: '0 1px 4px rgba(0, 0, 0, 0.25)',
        whiteSpace: 'nowrap',
      })
      const openPlan = () => {
        suppressPickRef.current = true
        map.openInfoWindow(
          new BMapGL.InfoWindow(
            `<div class="info-window"><b>${escapeHtml(p.title)}</b><br/><span>${escapeHtml(p.reason)}</span></div>`,
            { width: 260, title: ACTION_SHORT[p.action] ?? '规划建议' },
          ),
          point,
        )
      }
      ring.addEventListener('click', openPlan)
      label.addEventListener('click', openPlan)
      add(ring)
      add(label)
    })
  }, [ready, center, isochrone, showHeatmap, showBlindspots, blindCategory])

  // 热力层：栅格放大 + 双线性插值，裁剪在等时圈内。
  // 它是独立画布而非地图覆盖物——覆盖物只能画矢量，铺不出连续色场。
  useEffect(() => {
    const map = mapRef.current
    const canvas = heatRef.current
    const BMapGL = window.BMapGL
    if (!map || !canvas || !BMapGL) return

    const blindspots = isochrone?.properties.blindspots
    const tile =
      showHeatmap && blindspots
        ? buildHeatTile(blindspots.cells, blindspots.grid_spacing_m, blindspots.max_reach_s ?? 0)
        : null
    const ring = isochrone?.geometry.coordinates[0] ?? []

    const paint = () => {
      const width = canvas.clientWidth
      const height = canvas.clientHeight
      const dpr = window.devicePixelRatio || 1
      if (canvas.width !== Math.round(width * dpr)) {
        canvas.width = Math.round(width * dpr)
        canvas.height = Math.round(height * dpr)
      }
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)
      if (!tile) return

      ctx.save()
      if (ring.length > 2) {
        // 裁剪到等时圈：圈外没有测过，插值不该把颜色铺出去
        ctx.beginPath()
        ring.forEach(([lng, lat], i) => {
          const p = map.pointToPixel(new BMapGL.Point(lng, lat))
          if (i === 0) ctx.moveTo(p.x, p.y)
          else ctx.lineTo(p.x, p.y)
        })
        ctx.closePath()
        ctx.clip()
      }
      const nw = map.pointToPixel(new BMapGL.Point(tile.west, tile.north))
      const se = map.pointToPixel(new BMapGL.Point(tile.east, tile.south))
      ctx.imageSmoothingEnabled = true
      ctx.imageSmoothingQuality = 'high'
      ctx.drawImage(tile.image, nw.x, nw.y, se.x - nw.x, se.y - nw.y)
      ctx.restore()
    }

    paint()
    const events = ['moving', 'moveend', 'zooming', 'zoomend', 'resize']
    events.forEach((e) => map.addEventListener(e, paint))
    return () => events.forEach((e) => map.removeEventListener(e, paint))
  }, [ready, isochrone, showHeatmap])

  const blindspots = isochrone?.properties.blindspots
  const shownBlind = blindspots ? cellsMissing(blindspots.cells, blindCategory).length : 0
  const planCount = (isochrone?.properties.report?.prescriptions ?? []).filter(
    (p) => p.lat != null && p.lng != null,
  ).length

  return (
    <div className="map-shell">
      <div ref={containerRef} className="map-canvas" />
      <canvas ref={heatRef} className="heat-canvas" />
      <div className={`map-mode floating ${pickEnabled ? 'live' : 'browse'}`}>
        <i />
        {pickEnabled
          ? '点击地图将按新中心点重新计算（消耗配额）'
          : '浏览模式：点击盲区或规划圆点看详情，点击地图不会算路'}
      </div>
      {(blindspots || planCount > 0) && (
        <div className="map-legend floating">
          {showHeatmap && blindspots && (
            <div className="legend-row">
              <span className="legend-title">步行耗时</span>
              <span className="legend-scale">
                {HEAT_COLORS.map((c) => (
                  <i key={c} style={{ background: c }} />
                ))}
              </span>
              <span className="legend-ends">
                <em>近</em>
                <em>
                  {blindspots.max_reach_s !== null
                    ? `${Math.round(blindspots.max_reach_s / 60)} 分钟`
                    : '远'}
                </em>
              </span>
            </div>
          )}
          {showHeatmap && blindspots && (
            <div className="legend-row">
              <span className="legend-swatch unknown" />
              <span>灰色为测距失败，不是「很远」</span>
            </div>
          )}
          {showBlindspots && blindspots && (
            <div className="legend-row">
              <span
                className="legend-swatch blind"
                style={{
                  background: `${BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all}44`,
                  borderColor: BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all,
                }}
              />
              <span>
                {blindCategory === 'all' ? '服务盲区' : `${blindCategory}盲区`} {shownBlind} /{' '}
                {blindspots.cell_count} 个网格连片（点击看详情）
              </span>
            </div>
          )}
          {planCount > 0 && (
            <div className="legend-row">
              <span className="legend-swatch plan" />
              <span>规划建议 {planCount} 处（点击圆点看开方）</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** 轮廓是连片的，点哪儿就取最近的那个网格来解释，细节不因融合而丢失。 */
function nearestCell(cells: GridCell[], lat?: number, lng?: number): GridCell | null {
  if (cells.length === 0) return null
  if (lat === undefined || lng === undefined) return cells[0]
  let best = cells[0]
  let bestD = Number.POSITIVE_INFINITY
  for (const cell of cells) {
    const d = (cell.lat - lat) ** 2 + (cell.lng - lng) ** 2
    if (d < bestD) {
      bestD = d
      best = cell
    }
  }
  return best
}

/** 把相距不到 250 米的处方并成一簇，返回每条处方的标签锚点与簇内层号。 */
function labelAnchors(items: Prescription[]) {
  const clusters: { lat: number; lng: number; size: number }[] = []
  return items.map((p) => {
    const lat = p.lat as number
    const lng = p.lng as number
    const hit = clusters.find((c) => {
      const dLat = (c.lat - lat) * 111_320
      const dLng = (c.lng - lng) * 111_320 * Math.cos((lat * Math.PI) / 180)
      return Math.hypot(dLat, dLng) < 250
    })
    if (!hit) {
      clusters.push({ lat, lng, size: 1 })
      return { lat, lng, slot: 0 }
    }
    hit.size += 1
    return { lat: hit.lat, lng: hit.lng, slot: hit.size - 1 }
  })
}

/** 盲区详情的信息窗内容。距离取真实路网步行距离，不是直线距离。 */
function describeCell(cell: GridCell, limitM: number): string {
  const limitKm = (limitM / 1000).toFixed(1)
  const lines = [`步行 ${limitKm} 公里内缺：<b>${cell.missing.join('、')}</b>`]

  const known = Object.entries(cell.nearest_m).filter(([, v]) => v !== null)
  if (known.length > 0) {
    const detail = known
      .map(([name, v]) => `${name} ${Math.round(v as number)} 米`)
      .join('<br/>')
    lines.push(`<span>最近设施步行距离：</span><br/>${detail}`)
  }
  if (cell.unknown.length > 0) {
    lines.push(`<span>测距失败，无法判定：${cell.unknown.join('、')}</span>`)
  }
  if (cell.reach_s !== null) {
    lines.push(`<span>自中心点步行约 ${Math.round(cell.reach_s / 60)} 分钟</span>`)
  }
  return `<div class="info-window">${lines.join('<br/>')}</div>`
}
