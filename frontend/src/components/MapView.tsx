import { useEffect, useRef } from 'react'
import { loadBaiduMap } from '../baiduMap'
import type { GridCell, IsochroneFeature } from '../types'

interface Props {
  ak: string
  center: { lat: number; lng: number }
  isochrone: IsochroneFeature | null
  showHeatmap: boolean
  showBlindspots: boolean
  onPickCenter: (lat: number, lng: number) => void
  onError: (message: string) => void
  /** 关闭时点击地图不改中心、不算路，避免演示误触烧配额。 */
  pickEnabled: boolean
}

// 热力图色阶：由近及远。JS API GL 不自带热力图图层，
// 而网格判定本就产出规则点阵，直接把每个网格画成方块即可——
// 既不必引入 mapvgl 这类额外依赖，色块边界也正好对应判定粒度，不会因插值而虚化。
const HEAT_COLORS = ['#1a9850', '#91cf60', '#d9ef8b', '#fee08b', '#fc8d59', '#d73027']

function heatColor(reachS: number | null, maxS: number): string {
  if (reachS === null) return '#9ca3af' // 测距失败，灰色示意「未知」而非「很远」
  const t = maxS > 0 ? Math.min(1, reachS / maxS) : 0
  return HEAT_COLORS[Math.min(HEAT_COLORS.length - 1, Math.floor(t * HEAT_COLORS.length))]
}

/** 把网格点扩成正方形色块。经度方向的度距随纬度收缩，必须按纬度换算，否则方块会走形。 */
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

/**
 * 地图视图：渲染等时圈多边形、中心点标注，并支持点击改选中心点。
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
  onPickCenter,
  onError,
  pickEnabled,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<any>(null)
  const overlaysRef = useRef<any[]>([])
  // 点击回调里要用到最新的处理函数，但地图监听只注册一次，故用 ref 转发
  const pickRef = useRef(onPickCenter)
  pickRef.current = onPickCenter
  // 点击盲区色块只应弹出详情。若任其冒泡到地图，就会被当成「改选中心点」
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

  // 绘制热力网格、等时圈轮廓、盲区点位与中心点
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
    // 色阶按本次结果的最大耗时归一，而不是按时间阈值——
    // 障碍截断会让最远网格远低于阈值，按阈值归一时整张图会偏冷、看不出层次
    const maxReach = blindspots?.max_reach_s ?? 0

    if (blindspots && showHeatmap) {
      for (const cell of blindspots.cells) {
        const corners = cellCorners(cell, spacing).map(
          ([lng, lat]) => new BMapGL.Point(lng, lat),
        )
        add(
          new BMapGL.Polygon(corners, {
            strokeWeight: 0,
            strokeOpacity: 0,
            fillColor: heatColor(cell.reach_s, maxReach),
            fillOpacity: 0.55,
          }),
        )
      }
    }

    if (isochrone) {
      const ring = isochrone.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      const polygon = new BMapGL.Polygon(points, {
        strokeColor: '#1f6feb',
        strokeWeight: 2,
        strokeOpacity: 0.9,
        fillColor: '#1f6feb',
        // 叠了热力网格后填充会盖住色阶，此时只保留轮廓
        fillOpacity: blindspots && showHeatmap ? 0 : 0.18,
      })
      add(polygon)
      // 视野贴合等时圈范围，比固定缩放级别更实用——
      // 不同社区的可达范围差异很大，桃浦镇比曹杨新村小了近三成
      map.setViewport(points)
    } else {
      map.setCenter(new BMapGL.Point(center.lng, center.lat))
    }

    if (blindspots && showBlindspots) {
      for (const cell of blindspots.cells) {
        if (cell.missing.length === 0) continue
        const corners = cellCorners(cell, spacing).map(
          ([lng, lat]) => new BMapGL.Point(lng, lat),
        )
        const patch = new BMapGL.Polygon(corners, {
          strokeColor: '#b91c1c',
          strokeWeight: 2,
          strokeOpacity: 0.95,
          fillColor: '#dc2626',
          // 缺的品类越多填得越实，浏览时不必逐个点开也能看出严重程度
          fillOpacity: 0.2 + 0.2 * Math.min(3, cell.missing.length),
        })
        patch.addEventListener('click', () => {
          suppressPickRef.current = true
          map.openInfoWindow(
            new BMapGL.InfoWindow(describeCell(cell, blindspots.walk_limit_m), {
              width: 240,
              title: '服务盲区',
            }),
            new BMapGL.Point(cell.lng, cell.lat),
          )
        })
        add(patch)
      }
    }

    add(new BMapGL.Marker(new BMapGL.Point(center.lng, center.lat)))
  }, [center, isochrone, showHeatmap, showBlindspots])

  const blindspots = isochrone?.properties.blindspots
  return (
    <div className="map-shell">
      <div ref={containerRef} className="map-canvas" />
      <div className={`map-mode ${pickEnabled ? 'live' : 'browse'}`}>
        {pickEnabled
          ? '点击地图将按新中心点重新计算（消耗配额）'
          : '浏览模式：点击盲区色块看详情，点击地图不会算路'}
      </div>
      {blindspots && (
        <div className="map-legend">
          {showHeatmap && (
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
          {showHeatmap && (
            <div className="legend-row">
              <span className="legend-swatch unknown" />
              <span>灰色为测距失败，不是「很远」</span>
            </div>
          )}
          {showBlindspots && (
            <div className="legend-row">
              <span className="legend-swatch blind" />
              <span>
                服务盲区 {blindspots.blind_count} / {blindspots.cell_count} 个网格
                （点击色块看缺哪类）
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
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
