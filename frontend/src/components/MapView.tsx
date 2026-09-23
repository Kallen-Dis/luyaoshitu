import { useEffect, useRef, useState } from 'react'
import { loadBaiduMap } from '../baiduMap'
import { HEAT_COLORS, buildHeatTile } from '../lib/heatRaster'
import { WIDE_CELL_M, wideBlindCells } from '../lib/wideBlind'
import type { GridCell, IsochroneFeature, Place, SimulationResult } from '../types'

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
  /** 模拟新建结果：高亮被覆盖的盲区网格与拟建点位。 */
  simulation?: SimulationResult | null
  /** 两地对比的第二个等时圈（橙色渲染，与主圈的蓝色区分）。 */
  compare?: IsochroneFeature | null
  /** 图例里切换图层。 */
  onToggleHeatmap?: () => void
  onToggleBlindspots?: () => void
  blindCategories?: string[]
  onBlindCategory?: (name: string) => void
  /** 圈外设施标可关。 */
  showOutsidePlaces?: boolean
  onToggleOutsidePlaces?: () => void
  /** 对比选点时，地图点击是设对比地点。 */
  pickHint?: string
}

/** 盲区按品类分色。同时看三类会糊成一片，故界面上一次只画一层。 */
const BLIND_COLORS: Record<string, string> = {
  all: '#c0391d',
  生鲜采买: '#ea580c',
  医药: '#dc2626',
  基础教育: '#7c3aed',
}

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

/** 把网格点扩成正方形色块。用于标出测距失败或被模拟覆盖的网格。 */
function cellCorners(cell: { lat: number; lng: number }, spacingM: number) {
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
  simulation,
  compare,
  onToggleHeatmap,
  onToggleBlindspots,
  blindCategories = [],
  onBlindCategory,
  showOutsidePlaces = true,
  onToggleOutsidePlaces,
  pickHint,
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

    const places = [
      ...(isochrone?.properties.coverage?.places ?? []),
      // 拟建设施并进同一套方格：1 公里内缺这一类的格子不再画出，而不是另铺绿色方格。
      ...(simulation
        ? [
            {
              category: simulation.category,
              name: '拟建',
              lat: simulation.lat,
              lng: simulation.lng,
              in_circle: true,
            },
          ]
        : []),
    ]
    const wide =
      showBlindspots && isochrone?.properties.center && places.length > 0
        ? wideBlindCells(
            isochrone.properties.center,
            places,
            isochrone.geometry.coordinates[0] ?? [],
          ).filter((cell) => blindCategory === 'all' || cell.missing.includes(blindCategory))
        : null

    if (wide && isochrone?.properties.center) {
      const color = BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all
      for (const cell of wide) {
        const points = cellCorners(cell, WIDE_CELL_M).map(([lng, lat]) => new BMapGL.Point(lng, lat))
        const area = new BMapGL.Polygon(points, {
          strokeColor: color,
          strokeWeight: cell.inCircle ? 1 : 0.5,
          strokeOpacity: cell.inCircle ? 0.45 : 0.25,
          fillColor: color,
          fillOpacity: cell.inCircle ? 0.22 : 0.1,
        })
        area.setZIndex?.(2)
        const detail: GridCell = { ...cell, reach_s: null, nearest_m: {}, unknown: [] }
        area.addEventListener('click', () => {
          suppressPickRef.current = true
          // 百度多边形点击事件经常不带鼠标坐标，按事件再找「最近一格」会永远落到第一格。
          // 方格创建时就把自己绑上，弹窗跟这一格走。
          map.closeInfoWindow()
          map.openInfoWindow(
            new BMapGL.InfoWindow(describeCell(detail, blindspots?.walk_limit_m ?? 1000), {
              width: 250,
              title: blindCategory === 'all' ? '服务盲区' : `服务盲区·${blindCategory}`,
            }),
            new BMapGL.Point(cell.lng, cell.lat),
          )
        })
        add(area)
      }
    }

    // 等时圈轮廓画在网格之上：盲区连成片时，压在下面的边界会被整片红色吃掉
    const viewPoints: any[] = []
    if (isochrone) {
      const ring = isochrone.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      const outer = new BMapGL.Polygon(points, {
        strokeColor: '#1f7a42',
        strokeWeight: 1.5,
        strokeOpacity: 1,
        fillColor: '#2f9e5a',
        fillOpacity: 0.06,
        enableClicking: false,
      })
      outer.setZIndex?.(4)
      add(outer)
      viewPoints.push(...points)
      if (wide && isochrone.properties.center) {
        const c = isochrone.properties.center
        const dLat = 1500 / 111_320
        const dLng = 1500 / (111_320 * Math.cos((c.lat * Math.PI) / 180))
        viewPoints.push(
          new BMapGL.Point(c.lng - dLng, c.lat - dLat),
          new BMapGL.Point(c.lng + dLng, c.lat + dLat),
        )
      }

      for (const inner of isochrone.properties.rings ?? []) {
        const innerPoints = inner.coordinates.map(([lng, lat]) => new BMapGL.Point(lng, lat))
        const layer = new BMapGL.Polygon(innerPoints, {
          strokeColor: '#1f7a42',
          strokeWeight: inner.minutes <= 5 ? 1 : 1.25,
          strokeOpacity: 0.95,
          fillColor: '#2f9e5a',
          fillOpacity: inner.minutes <= 5 ? 0.28 : 0.16,
          enableClicking: false,
        })
        layer.setZIndex?.(inner.minutes <= 5 ? 8 : 6)
        add(layer)
      }

      // 设施标放在方格和等时圈之上。桃浦圈内设施为 0，点都在圈外，
      // 若压在盲区方格下面，打开样例就像一张空图。
      for (const place of isochrone.properties.coverage?.places ?? []) {
        if (!showOutsidePlaces && !place.in_circle) continue
        add(placeMarker(BMapGL, map, place, suppressPickRef))
      }
    }

    // 两地对比：B 圈用橙色与主圈的蓝色区分，并标注 B 的中心名称
    if (compare) {
      const ring = compare.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      add(
        new BMapGL.Polygon(points, {
          strokeColor: '#b0801f',
          strokeWeight: 1.5,
          strokeOpacity: 0.95,
          strokeStyle: 'dashed',
          fillColor: '#b0801f',
          fillOpacity: 0.08,
        }),
      )
      viewPoints.push(...points)
      const bCenter = compare.properties.center
      if (bCenter) {
        const bLabel = new BMapGL.Label(
          `B · ${(compare.properties.name ?? '').replace(/^上海市普陀区/, '') || '对比地点'}`,
          { position: new BMapGL.Point(bCenter.lng, bCenter.lat), offset: new BMapGL.Size(-40, -14) },
        )
        bLabel.setStyle({
          color: '#fff',
          background: '#c2410c',
          border: '1px solid rgba(255, 255, 255, 0.85)',
          borderRadius: '10px',
          padding: '2px 8px',
          fontSize: '11px',
          fontWeight: '600',
          boxShadow: '0 1px 4px rgba(0, 0, 0, 0.25)',
          whiteSpace: 'nowrap',
        })
        add(bLabel)
      }
    }

    if (viewPoints.length > 0) {
      // 视野贴合圈范围，比固定缩放级别更实用——
      // 不同社区的可达范围差异很大，桃浦镇比曹杨新村小了近三成
      map.setViewport(viewPoints)
    } else {
      map.setCenter(new BMapGL.Point(center.lng, center.lat))
    }

    add(new BMapGL.Marker(new BMapGL.Point(center.lng, center.lat)))

    // 拟建点只标位置和 1 公里判定圈。圈内原本缺这一类的方格已经从盲区层拿掉。
    if (simulation) {
      const simPoint = new BMapGL.Point(simulation.lng, simulation.lat)
      const reach = new BMapGL.Circle(simPoint, 1000, {
        strokeColor: '#c0391d',
        strokeWeight: 1.2,
        strokeOpacity: 0.55,
        strokeStyle: 'dashed',
        fillColor: '#c0391d',
        fillOpacity: 0.04,
      })
      reach.disableMassClear?.()
      add(reach)
      const simLabel = new BMapGL.Label(`拟建·${simulation.category}`, {
        position: simPoint,
        offset: new BMapGL.Size(-36, -46),
      })
      simLabel.setStyle({
        color: '#1f7a42',
        background: '#f3faf4',
        border: '1px solid rgba(255, 255, 255, 0.85)',
        borderRadius: '10px',
        padding: '2px 8px',
        fontSize: '11px',
        fontWeight: '600',
        boxShadow: '0 1px 4px rgba(0, 0, 0, 0.25)',
        whiteSpace: 'nowrap',
      })
      add(simLabel)
    }
  }, [ready, center, isochrone, showHeatmap, showBlindspots, blindCategory, showOutsidePlaces, simulation, compare])

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
        ? buildHeatTile(
            blindspots.cells,
            blindspots.grid_spacing_m,
            blindspots.max_reach_s ?? 0,
            0.32,
          )
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
      if (tile && ring.length > 2) {
        ctx.save()
        ctx.beginPath()
        ring.forEach(([lng, lat], i) => {
          const p = map.pointToPixel(new BMapGL.Point(lng, lat))
          if (i === 0) ctx.moveTo(p.x, p.y)
          else ctx.lineTo(p.x, p.y)
        })
        ctx.closePath()
        ctx.clip()
        const nw = map.pointToPixel(new BMapGL.Point(tile.west, tile.north))
        const se = map.pointToPixel(new BMapGL.Point(tile.east, tile.south))
        ctx.imageSmoothingEnabled = true
        ctx.imageSmoothingQuality = 'high'
        ctx.drawImage(tile.image, nw.x, nw.y, se.x - nw.x, se.y - nw.y)
        ctx.restore()
      }
      const strokeRing = (coords: [number, number][], widthPx: number, alpha: number) => {
        if (coords.length < 3) return
        ctx.beginPath()
        coords.forEach(([lng, lat], i) => {
          const p = map.pointToPixel(new BMapGL.Point(lng, lat))
          if (i === 0) ctx.moveTo(p.x, p.y)
          else ctx.lineTo(p.x, p.y)
        })
        ctx.closePath()
        ctx.strokeStyle = `rgba(31, 122, 66, ${alpha})`
        ctx.lineWidth = widthPx
        ctx.stroke()
      }
      for (const inner of isochrone?.properties.rings ?? []) {
        strokeRing(inner.coordinates, inner.minutes <= 5 ? 1 : 1.25, 0.75)
      }
      strokeRing(ring, 1.5, 1)
    }

    paint()
    const events = ['moving', 'moveend', 'zooming', 'zoomend', 'resize']
    events.forEach((e) => map.addEventListener(e, paint))
    return () => events.forEach((e) => map.removeEventListener(e, paint))
  }, [ready, isochrone, showHeatmap])

  const [legendOpen, setLegendOpen] = useState(true)
  const blindspots = isochrone?.properties.blindspots
  const shownBlind = blindspots ? cellsMissing(blindspots.cells, blindCategory).length : 0

  return (
    <div className="map-shell">
      <div ref={containerRef} className="map-canvas" />
      <canvas ref={heatRef} className="heat-canvas" />
      {pickEnabled && (
        <div className="map-mode floating live">
          <i />
          {pickHint ?? '点击地图将按新中心点重新计算（消耗配额）'}
        </div>
      )}
      <button
        type="button"
        className="legend-toggle floating"
        aria-expanded={legendOpen}
        onClick={() => setLegendOpen((open) => !open)}
      >
        图例
      </button>
      {legendOpen && (
        <div className="map-legend floating">
          <div className="legend-row">
            <span className="legend-swatch ring-outer" />
            <span>15 分钟等时圈</span>
          </div>
          {(isochrone?.properties.rings ?? []).map((r) => (
            <div className="legend-row" key={r.minutes}>
              <span className="legend-swatch ring-inner" />
              <span>{r.minutes} 分钟内圈</span>
            </div>
          ))}
          <div className="legend-row">
            <span className="legend-swatch blind" />
            <span>服务盲区方格</span>
          </div>
          <div className="legend-marks">
            {Object.entries(PLACE_MARK).map(([name, mark]) => (
              <span key={name} className="legend-mark">
                <i style={{ background: mark.color }}>{mark.glyph}</i>
                {name}
              </span>
            ))}
          </div>
          <label className="legend-row">
            <input type="checkbox" checked={showHeatmap} onChange={() => onToggleHeatmap?.()} />
            <span>步行耗时</span>
          </label>
          <label className="legend-row">
            <input
              type="checkbox"
              checked={showBlindspots}
              onChange={() => onToggleBlindspots?.()}
            />
            <span>服务盲区（中心 1.5 公里）</span>
          </label>
          <label className="legend-row">
            <input
              type="checkbox"
              checked={showOutsidePlaces}
              onChange={() => onToggleOutsidePlaces?.()}
            />
            <span>圈外设施</span>
          </label>
          {showBlindspots && blindCategories.length > 0 && (
            <div className="legend-pills">
              <button
                type="button"
                className={blindCategory === 'all' ? 'pill active' : 'pill'}
                onClick={() => onBlindCategory?.('all')}
              >
                缺任一类
              </button>
              {blindCategories.map((name) => (
                <button
                  key={name}
                  type="button"
                  className={blindCategory === name ? 'pill active' : 'pill'}
                  onClick={() => onBlindCategory?.(name)}
                >
                  {name}
                </button>
              ))}
            </div>
          )}
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
                {blindspots.cell_count} 个方格（点击看详情）
              </span>
            </div>
          )}
          {simulation && (
            <div className="legend-row">
              <span className="legend-swatch plan" />
              <span>拟建点 · 虚线圈内方格已消去</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

const PLACE_MARK: Record<string, { glyph: string; color: string }> = {
  生鲜采买: { glyph: '菜', color: '#c05621' },
  医药: { glyph: '药', color: '#2f7d32' },
  基础教育: { glyph: '学', color: '#1d4e89' },
  基础医疗: { glyph: '医', color: '#0f766e' },
  养老服务: { glyph: '养', color: '#7c3aed' },
  文体休闲: { glyph: '文', color: '#b45309' },
}

function placeMarker(
  BMapGL: any,
  map: any,
  place: Place,
  suppressPickRef: { current: boolean },
) {
  const mark = PLACE_MARK[place.category] ?? { glyph: '·', color: '#4b4e45' }
  const label = new BMapGL.Label(mark.glyph, {
    position: new BMapGL.Point(place.lng, place.lat),
    offset: new BMapGL.Size(-11, -11),
  })
  label.setStyle({
    color: '#fff',
    background: mark.color,
    border: '2px solid #f3faf4',
    borderRadius: '50%',
    width: '22px',
    height: '22px',
    lineHeight: '18px',
    textAlign: 'center',
    fontSize: '11px',
    fontWeight: '700',
    opacity: place.in_circle ? '1' : '0.82',
    cursor: 'pointer',
  })
  label.setZIndex?.(90)
  label.addEventListener('click', () => {
    suppressPickRef.current = true
    map.openInfoWindow(
      new BMapGL.InfoWindow(
        `${place.in_circle ? '在 15 分钟圈内' : '在附近，走不进 15 分钟圈'}`,
        { width: 220, title: `${place.category} · ${place.name}` },
      ),
      new BMapGL.Point(place.lng, place.lat),
    )
  })
  return label
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
