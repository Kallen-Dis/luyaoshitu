import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { loadBaiduMap } from '../baiduMap'
import { buildHeatTile, heatColor } from '../lib/heatRaster'
import { blindCells } from '../lib/grid'
import { clipBlindCell, clipRegionOutlines } from '../lib/grayDisplay'
import { tripPath, tripColor } from '../lib/trip'
import { hitTripRoute, itineraryLineStyle, itineraryPath, tripCameraRequest, tripSegmentColor, tripViewBounds, tripViewportMargins, type TripViewStamp } from '../lib/itinerary'
import { afterStableMapSize, originViewBounds } from '../lib/guide'
import { TYPE_META, circleHitsPath, metersBetween, type LatLng } from '../lib/markings'
import type { ResultView, ViewSwitch } from '../lib/resultView'
import type { Draft } from './markings/draft'
import {
  BLIND_COLORS,
  PLACE_MARK,
  heatScaleS,
  type LayerState,
  type MapContext,
  type MapIntent,
} from './map/context'
import { MapLegend } from './map/MapLegend'
import { MapPopover } from './map/MapPopover'
import { MapSceneBar } from './map/MapSceneBar'
import { MapViewBar } from './map/MapViewBar'
import { MapOutlines, type MapOutline } from './map/MapOutlines'
import { TripMapStops } from './map/TripMapStops'
import { useMapScene } from './map/useMapScene'
import './map/map.css'
import type {
  ClosureSpec,
  ConstructionCandidate,
  GridCell,
  IsochroneFeature,
  Marking,
  Place,
  RecheckRay,
  RecheckSuspect,
  SimulationResult,
  TripMapData,
  TripGuideData,
} from '../types'

interface Props {
  trip?: TripMapData | null
  tripOpen?: boolean
  guide?: TripGuideData | null
  onTripSelect?: (entryId: string | null) => void
  ak: string
  center: { lat: number; lng: number }
  /** 地图上画的结果（「纯算法」看法下已还原成纯算法结果） */
  isochrone: IsochroneFeature | null
  /** 视野只在它变了时贴合；切换「纯算法 / 含标注」不应改变用户当前的缩放与平移 */
  fitKey?: unknown
  layers: LayerState
  onLayers: (next: LayerState) => void
  /** 'all' 表示「缺任一关键设施」，否则为某个品类名。 */
  blindCategory: string
  blindCategories?: string[]
  onBlindCategory?: (name: string) => void
  onPickCenter: (lat: number, lng: number) => void
  onError: (message: string) => void
  /** 关闭时点击地图不改中心、不算路，避免演示误触烧配额。 */
  pickEnabled: boolean
  /** 对比选点时，地图点击是设对比地点。 */
  pickHint?: string
  /** 模拟新建结果：高亮被覆盖的盲区网格与拟建点位。 */
  simulation?: SimulationResult | null
  /** 两地对比的第二个等时圈（橙色渲染，与主圈区分）。 */
  compare?: IsochroneFeature | null
  /** 用户画的临时围挡（含尚未参与计算的）。 */
  closures?: ClosureSpec[]
  /** 复测巡检发现的疑似新增阻断（尚未确认）。 */
  suspects?: RecheckSuspect[]
  /** 复测里路线有变化的方向：旧路线灰虚线、新路线彩线。 */
  recheckRays?: RecheckRay[]
  /** 工地 POI 候选（尚未确认）。 */
  constructionSites?: ConstructionCandidate[]
  /** 附近的共享标注（待核实 + 已核实）。 */
  markings?: Marking[]
  selectedMarkingId?: number | null
  onSelectMarking?: (id: number) => void
  /** 正在画的标注草图；有值时地图进入「标注模式」：无关图层淡化，点击交给草图 */
  draft?: Draft | null
  /** 画「设施失效」时点了地图上的设施标 */
  onPickPlace?: (place: Place) => void
  /** 拖动围挡圆边上的手柄改了半径 */
  onDraftRadius?: (radiusM: number) => void
  radiusLimits?: [number, number]
  /** 把视野移到这里（key 变了才移动） */
  focus?: { lat: number; lng: number; key: number } | null
  /** 确保这个点不被右侧抽屉挡住（key 变了才检查） */
  reveal?: { lat: number; lng: number; key: number; rightInset: number } | null
  /** 另一种看法的外圈，画成点线对比 */
  altRing?: [number, number][] | null
  viewSwitch: ViewSwitch
  resultView: ResultView
  onResultView: (view: ResultView) => void
  onRerun: (mode: 'auto' | 'none') => void
  busy?: boolean
  /** 共享标注可用：点地图上的东西时给出标注动作 */
  canMark?: boolean
  onIntent?: (intent: MapIntent) => void
}

/** 测距失败的方格用浅蓝，避免和「灰色区域」混淆。 */
const UNKNOWN_COLOR = '#93c5fd'
/** 灰色区域外框与编号的颜色。 */
const REGION_COLOR = '#374151'
/** 复测线索的颜色：紫色表示「待确认」，与已确认围挡的橙褐色区分。 */
const SUSPECT_COLOR = '#7c3aed'
const SITE_COLOR = '#b45309'
/** 草图描边比已有标注粗，一眼分清哪个是正在画的。 */
const DRAFT_STROKE = 3
/** 标注模式下无关图层的透明度系数 */
const DIM = 0.35
/** 拖完半径手柄后这段时间内的地图点击视为拖动的余波，不算放点 */
const DRAG_GRACE_MS = 350

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
}

/** 把网格点扩成正方形色块。 */
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

/** 以中心为圆心、沿正北推进 meters 米后的点，用于把网格范围换算成像素半径。 */
function northOf(center: LatLng, meters: number) {
  return { lat: center.lat + meters / 111_320, lng: center.lng }
}

/** 正东 meters 米处：围挡半径手柄放在这里。 */
function eastOf(center: LatLng, meters: number) {
  return { lat: center.lat, lng: center.lng + meters / (111_320 * Math.cos((center.lat * Math.PI) / 180)) }
}

function handleIcon(color: string) {
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">` +
    `<circle cx="10" cy="10" r="7.5" fill="#fff" stroke="${color}" stroke-width="3"/>` +
    `<circle cx="10" cy="10" r="2.2" fill="${color}"/></svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}

// 默认值必须是模块级常量：写成 `closures = []` 每次渲染都是新数组，
// 绘制 effect 的依赖随之每次变化，整层覆盖物会在每次渲染时重建
const NO_CLOSURES: ClosureSpec[] = []
const NO_SUSPECTS: RecheckSuspect[] = []
const NO_RAYS: RecheckRay[] = []
const NO_SITES: ConstructionCandidate[] = []
const NO_MARKINGS: Marking[] = []
const NO_CATEGORIES: string[] = []
const DEFAULT_RADIUS_LIMITS: [number, number] = [10, 300]

interface PopoverState {
  id: number
  /** 弹出时的结果；结果一换（新计算、切换看法）卡片随之失效 */
  owner: IsochroneFeature | null
  ctx: MapContext
  at: LatLng
  x: number
  y: number
}

/**
 * 地图视图：渲染等时圈、步行耗时热力图、服务盲区与规划建议，并支持点击改选中心点。
 *
 * 点方格、设施、疑似点、灰色区域编号或空白处，弹出就地卡片（MapPopover），
 * 说明这里的判定并给出能做的标注。画标注草图时进入「标注模式」：无关图层淡化，
 * 围挡草图标出被挡住的步行路线，圆边上有拖动半径的手柄。
 *
 * 百度地图实例通过 ref 持有而非放进 state——它是命令式的可变对象，
 * 放进 state 会触发无意义的重渲染，还可能导致地图被反复销毁重建。
 */
export function MapView({
  trip = null,
  tripOpen = false,
  guide = null,
  onTripSelect,
  ak,
  center,
  isochrone,
  fitKey,
  layers,
  onLayers,
  blindCategory,
  blindCategories = NO_CATEGORIES,
  onBlindCategory,
  onPickCenter,
  onError,
  pickEnabled,
  pickHint,
  simulation,
  compare,
  closures = NO_CLOSURES,
  suspects = NO_SUSPECTS,
  recheckRays = NO_RAYS,
  constructionSites = NO_SITES,
  markings = NO_MARKINGS,
  selectedMarkingId = null,
  onSelectMarking,
  draft = null,
  onPickPlace,
  onDraftRadius,
  radiusLimits = DEFAULT_RADIUS_LIMITS,
  focus = null,
  reveal = null,
  altRing = null,
  viewSwitch,
  resultView,
  onResultView,
  onRerun,
  busy = false,
  canMark = false,
  onIntent,
}: Props) {
  const shellRef = useRef<HTMLDivElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const heatRef = useRef<HTMLCanvasElement>(null)
  const mapRef = useRef<any>(null)
  const overlaysRef = useRef<any[]>([])
  const markingOverlaysRef = useRef<any[]>([])
  const tripOverlaysRef = useRef<any[]>([])
  const tripFitRef = useRef<TripViewStamp | null>(null)
  const tripDataRef = useRef(trip)
  const tripOpenRef = useRef(tripOpen)
  const tripSelectRef = useRef(onTripSelect)
  const guideActive = guide !== null
  const guideActiveRef = useRef(guideActive)
  const guideViewRef = useRef<{ owner: IsochroneFeature | null; center: any; zoom: number; tilt: number; heading: number; earth: boolean } | null>(null)
  const tripViewRef = useRef<{ owner: IsochroneFeature | null; center: any; zoom: number; tilt: number; heading: number; earth: boolean } | null>(null)
  const guideFocusRef = useRef<number | null>(null)
  // 地图实例是异步建好的，而快照往往先到：只用 ref 持有实例的话，
  // 绘制 effect 会在实例就绪前空跑一次，此后再无触发，首屏就成了一张白底底图。
  const [ready, setReady] = useState(false)
  const [shell, setShell] = useState({ w: 0, h: 0 })
  const [popover, setPopover] = useState<PopoverState | null>(null)
  const [legendOpen, setLegendOpen] = useState(false)
  // 标注模式下图例默认收起，用户点开才显示，退出后回到原来的开合
  const [drawLegend, setDrawLegend] = useState(false)
  // 视角、底图与全屏。全屏放大整个舞台（含搜索框与关键读数），找不到舞台就只放大地图
  const scene = useMapScene(mapRef, ready, () => {
    const shell = shellRef.current
    return shell?.closest<HTMLElement>('.stage') ?? shell
  })
  // 倾斜、旋转或卫星底图下，按像素叠加的色场画布对不上地面：热力改由地图自己画的矢量方格表达
  const vectorHeat = scene.angled || scene.earth
  const { earth: sceneEarth, setEarth: setSceneEarth } = scene

  // 点击回调里要用到最新的处理函数与开关，但地图监听只注册一次，故用 ref 转发
  const pickRef = useRef(onPickCenter)
  const pickEnabledRef = useRef(pickEnabled)
  const drawingRef = useRef<Draft['type'] | null>(null)
  const pickPlaceRef = useRef(onPickPlace)
  const selectMarkingRef = useRef(onSelectMarking)
  const radiusRef = useRef(onDraftRadius)
  const canMarkRef = useRef(canMark)
  const ownerRef = useRef(isochrone)
  const popoverOpenRef = useRef(false)
  useEffect(() => {
    pickRef.current = onPickCenter
    pickEnabledRef.current = pickEnabled
    drawingRef.current = draft?.type ?? null
    pickPlaceRef.current = onPickPlace
    selectMarkingRef.current = onSelectMarking
    radiusRef.current = onDraftRadius
    canMarkRef.current = canMark
    ownerRef.current = isochrone
    guideActiveRef.current = guideActive
    tripDataRef.current = trip
    tripOpenRef.current = tripOpen
    tripSelectRef.current = onTripSelect
  })
  // 点击覆盖物只应弹出详情。若任其冒泡到地图，就会被当成「改选中心点」
  // 而触发一次完整的实时计算——白烧配额，且用户根本没打算换点。
  const suppressPickRef = useRef(false)
  const dragEndAtRef = useRef(0)
  const viewKeyRef = useRef<unknown[]>([])
  const lastCenterRef = useRef<any>(null)
  const popSeqRef = useRef(0)

  const openPopover = useCallback((ctx: MapContext, at: LatLng) => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL) return
    const px = map.pointToPixel(new BMapGL.Point(at.lng, at.lat))
    popSeqRef.current += 1
    setPopover({ id: popSeqRef.current, owner: ownerRef.current, ctx, at, x: px.x, y: px.y })
  }, [])

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
        // 缩放控件钉在右下角固定位置，视角按钮（.map-scene）摞在它上面
        const anchor = (window as unknown as Record<string, unknown>).BMAP_ANCHOR_BOTTOM_RIGHT
        map.addControl(
          anchor !== undefined
            ? new BMapGL.ZoomControl({ anchor, offset: new BMapGL.Size(16, 24) })
            : new BMapGL.ZoomControl(),
        )
        lastCenterRef.current = map.getCenter()
        const remember = () => {
          lastCenterRef.current = map.getCenter()
        }
        map.addEventListener('moveend', remember)
        map.addEventListener('zoomend', remember)
        map.addEventListener('click', (e: any) => {
          if (guideActiveRef.current) return
          if (suppressPickRef.current) {
            suppressPickRef.current = false
            return
          }
          if (Date.now() - dragEndAtRef.current < DRAG_GRACE_MS) return
          // 有卡片开着：这一下是关掉它，不做别的
          if (popoverOpenRef.current) {
            setPopover(null)
            return
          }
          if (!e.latlng) return
          if (pickEnabledRef.current) {
            pickRef.current(e.latlng.lat, e.latlng.lng)
            return
          }
          const itinerary = tripDataRef.current
          if (itinerary?.itinerary && !drawingRef.current) {
            const pixel = map.pointToPixel(new BMapGL.Point(e.latlng.lng, e.latlng.lat))
            const id = hitTripRoute(itinerary.items, itinerary.selected, pixel, ([lng, lat]) => map.pointToPixel(new BMapGL.Point(lng, lat)), itinerary.preview)
            if (id) { tripSelectRef.current?.(id); return }
          }
          if (tripOpenRef.current && !drawingRef.current) return
          // 平时点空白处：问一句「这里缺了什么」，就地标注
          if (canMarkRef.current && !drawingRef.current) {
            openPopover({ kind: 'point', lat: e.latlng.lat, lng: e.latlng.lng }, e.latlng)
          }
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

  // 容器尺寸变了（窗口缩放、进出标注模式时侧栏收起 / 展开）：重铺地图并保持原来的中心
  useEffect(() => {
    const el = shellRef.current
    if (!el) return
    const ro = new ResizeObserver(() => {
      setShell({ w: el.clientWidth, h: el.clientHeight })
      const map = mapRef.current
      if (!map) return
      const keep = lastCenterRef.current
      if (typeof map.checkResize === 'function') map.checkResize()
      else if (typeof map.resize === 'function') map.resize()
      if (keep) map.setCenter(keep)
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const dimmed = draft !== null || tripOpen
  const dimPlaces = dimmed && draft?.type !== 'facility_missing'
  const grayOutlines = useMemo(() => {
    if (!layers.regions || !isochrone?.properties.blindspots || blindCategory !== 'all' || simulation) return []
    const circle = isochrone.geometry.coordinates[0] ?? []
    return (isochrone.properties.report?.gray_regions?.regions ?? []).flatMap(region => {
      const rings = clipRegionOutlines(region.rings, circle)
      return rings.length
        ? [{ region, rings, weight: region.id ? 2 : 1.5, anchor: region.id ? region.anchor : undefined }]
        : []
    })
  }, [layers.regions, isochrone, blindCategory, simulation])
  const mapOutlines = useMemo<MapOutline[]>(() => {
    const outlines: MapOutline[] = []
    const circle = (ring: [number, number][], weight: number, color: string, alpha = 1, dash?: number[]) => {
      if (ring.length < 3) return
      outlines.push({ rings: [ring], weight, color, alpha, dash, haloWidth: 1.5 })
    }
    if (isochrone) {
      if (isochrone.properties.delay?.applied && isochrone.properties.raw_ring) {
        circle(isochrone.properties.raw_ring, 1.2, '#1f7a42', 0.7, [5, 4])
      }
      if (altRing) circle(altRing, 1.6, '#475569', 0.85, [1.5, 4])
      circle(isochrone.geometry.coordinates[0] ?? [], 1.75, '#1f7a42')
      if (layers.innerRings) {
        for (const inner of isochrone.properties.rings ?? []) {
          circle(inner.coordinates, inner.minutes <= 5 ? 1.2 : 1.4, '#1f7a42', 0.95)
        }
      }
    }
    if (compare) circle(compare.geometry.coordinates[0] ?? [], 1.75, '#b0801f', 0.95, [7, 4])
    outlines.push(...grayOutlines.map(outline => ({ ...outline, color: REGION_COLOR, haloWidth: 1 })))
    return outlines
  }, [isochrone, layers.innerRings, altRing, compare, grayOutlines])
  const guideOutlines = useMemo<MapOutline[]>(() => {
    if (!guide?.item.route) return []
    const route = guide.item.route
    const paths: MapOutline[] = [{ rings: [route.path], closed: false, weight: 4.5, color: '#1f7a42', haloWidth: 3 }]
    const stepPath = route.steps[guide.stepIndex]?.path
    if (stepPath && stepPath.length > 1) paths.push({ rings: [stepPath], closed: false, weight: 5, color: '#1677d2', haloWidth: 2 })
    paths.push(...route.connectors.map(path => ({ rings: [path], closed: false, weight: 2, color: '#6b7280', dash: [4, 4], haloWidth: 2 })))
    return paths
  }, [guide])
  const itineraryOutlines = useMemo<MapOutline[]>(() => {
    if (!trip?.itinerary || guide) return []
    const highlight = trip.hover ?? trip.selected
    const items = [...trip.items].sort((a, b) => Number(a.entry_id === highlight) - Number(b.entry_id === highlight))
    return items.flatMap(item => {
      const style = itineraryLineStyle(item, item.entry_id === highlight, Boolean(highlight), trip.preview, trip.items.indexOf(item))
      const path = itineraryPath(item, trip.preview)
      const paths: MapOutline[] = [{ rings: [path], closed: false, ...style, dash: style.dashed ? [6, 5] : undefined, haloWidth: 3 }]
      paths.push(...(item.route?.connectors ?? []).map(path => ({ rings: [path], closed: false, weight: 2, color: '#6b7280', dash: [4, 4], haloWidth: 2 })))
      if (item.route?.blocked_path.length) paths.push({ rings: [item.route.blocked_path], closed: false, weight: 4, color: '#c0392b', dash: [6, 5], haloWidth: 2 })
      return paths
    })
  }, [trip, guide])

  // 绘制热力图、盲区方格、灰色区域、等时圈、设施与各类线索
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL) return

    overlaysRef.current.forEach((o) => map.removeOverlay(o))
    overlaysRef.current = []
    if (guideActive || tripOpen) return

    const add = (overlay: any) => {
      map.addOverlay(overlay)
      overlaysRef.current.push(overlay)
    }
    const fade = dimmed ? DIM : 1

    const blindspots = isochrone?.properties.blindspots
    const spacing = blindspots?.grid_spacing_m ?? 150
    const circle = isochrone?.geometry.coordinates[0] ?? []

    // 测距失败与受围挡阻断的网格单独标出来：热力层里它们没有值，不能让人误读成「很近」
    if (blindspots && layers.heat) {
      for (const cell of blindspots.cells) {
        if (cell.reach_s !== null) continue
        const corners = cellCorners(cell, spacing).map(([lng, lat]) => new BMapGL.Point(lng, lat))
        add(
          new BMapGL.Polygon(corners, {
            strokeWeight: 0,
            fillColor: cell.closure_blocked ? '#7c2d12' : UNKNOWN_COLOR,
            fillOpacity: (cell.closure_blocked ? 0.35 : 0.5) * fade,
            enableClicking: false,
          }),
        )
      }
    }

    // 3D / 卫星视角下的热力：每格一块矢量方格，由地图按透视画，倾斜旋转都贴着地面。
    // 平面视角仍用下面的画布色场（格子之间平滑过渡，更好看）
    if (blindspots && layers.heat && vectorHeat) {
      const maxS = heatScaleS(blindspots.max_reach_s, isochrone?.properties.minutes)
      for (const cell of blindspots.cells) {
        if (cell.reach_s === null) continue
        const corners = cellCorners(cell, spacing).map(([lng, lat]) => new BMapGL.Point(lng, lat))
        const tile = new BMapGL.Polygon(corners, {
          strokeWeight: 0,
          fillColor: heatColor(cell.reach_s, maxS),
          fillOpacity: 0.42 * fade,
          enableClicking: false,
        })
        tile.setZIndex?.(1)
        add(tile)
      }
    }

    // 盲区方格直接读后端的路网判定；拟建设施消去的格子由模拟结果给出（同样是路网测距）
    const wide = layers.blind && blindspots ? blindCells(blindspots, blindCategory, simulation) : null
    if (wide && blindspots) {
      const color = BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all
      // 「缺任一类」的格子连成片后由外框勾出区域，格线就不画了，否则一片灰里全是细线
      const gray = blindCategory === 'all'
      for (const { cell, missing } of wide) {
        const inside = cell.in_circle ?? true
        const parts = clipBlindCell(cellCorners(cell, spacing), circle)
        for (const part of parts) {
          const points = part.map(([lng, lat]) => new BMapGL.Point(lng, lat))
          const area = new BMapGL.Polygon(points, {
            strokeColor: color,
            strokeWeight: gray ? 0 : inside ? 1 : 0.5,
            strokeOpacity: (gray ? 0 : inside ? 0.45 : 0.25) * fade,
            fillColor: color,
            fillOpacity: (gray ? 0.2 : inside ? 0.22 : 0.1) * fade,
          })
          area.setZIndex?.(2)
          const detail: GridCell = { ...cell, missing }
          area.addEventListener('click', () => {
            // 画草图、放临时围挡、选对比点时，这一下是在点地图，交给地图点击处理，不弹卡片
            if (drawingRef.current || pickEnabledRef.current) return
            suppressPickRef.current = true
            // 百度多边形点击事件经常不带鼠标坐标，按事件再找「最近一格」会永远落到第一格。
            // 方格创建时就把自己绑上，卡片跟这一格走。
            openPopover({ kind: 'cell', cell: detail, missing }, cell)
          })
          add(area)
        }
      }
    }

    // 灰色区域：后端把相邻的盲区格并成片、编号并诊断成因，这里勾外框、标编号。
    // 模拟新建时方格已被消去一部分，而区域是按新建前算的，框会对不上，故不画。
    for (const { region, rings, weight } of grayOutlines) {
      // 平面外框由独立画布绘制，避免被建筑遮断；3D / 卫星沿用地面覆盖物。
      if (vectorHeat) {
        for (const ring of rings) {
          const pts = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
          const outline = new BMapGL.Polyline(pts, {
            strokeColor: REGION_COLOR,
            strokeWeight: weight,
            strokeOpacity: fade,
            strokeStyle: 'solid',
            enableClicking: false,
          })
          outline.setZIndex?.(3)
          add(outline)
        }
      }
      if (!region.id) continue
      const anchor = new BMapGL.Point(region.anchor.lng, region.anchor.lat)
      const tag = new BMapGL.Label(escapeHtml(region.id), {
        position: anchor,
        offset: new BMapGL.Size(-11, -11),
      })
      tag.setStyle({
        color: '#fff',
        background: REGION_COLOR,
        border: '2px solid #f9fafb',
        borderRadius: '6px',
        width: '22px',
        height: '22px',
        lineHeight: '18px',
        textAlign: 'center',
        fontSize: '12px',
        fontWeight: '700',
        cursor: 'pointer',
        opacity: String(fade),
        boxShadow: '0 1px 4px rgba(0, 0, 0, 0.3)',
      })
      tag.setZIndex?.(92)
      tag.addEventListener('click', () => {
        if (drawingRef.current || pickEnabledRef.current) return
        suppressPickRef.current = true
        openPopover({ kind: 'region', region }, region.anchor)
      })
      add(tag)
    }

    // 保留等时圈填充；平面轮廓统一交给独立画布，3D / 卫星使用地图地面覆盖物。
    const viewPoints: any[] = []
    if (isochrone) {
      const ring = isochrone.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      const outer = new BMapGL.Polygon(points, {
        strokeColor: '#1f7a42',
        strokeWeight: vectorHeat ? 1.5 : 0,
        strokeOpacity: vectorHeat ? 1 : 0,
        fillColor: '#2f9e5a',
        fillOpacity: 0.06,
        enableClicking: false,
      })
      outer.setZIndex?.(4)
      add(outer)
      viewPoints.push(...points)

      // 未计过街等待的圈画成虚线：两圈之间那一带，就是红绿灯与过街设施吃掉的可达范围
      const rawRing = isochrone.properties.delay?.applied ? isochrone.properties.raw_ring : null
      if (vectorHeat && rawRing && rawRing.length > 2) {
        const raw = new BMapGL.Polyline(
          rawRing.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
          { strokeColor: '#1f7a42', strokeWeight: 1.2, strokeOpacity: 0.7, strokeStyle: 'dashed' },
        )
        raw.setZIndex?.(5)
        add(raw)
      }

      // 另一种看法的圈画成石板灰点线：两圈之差就是共享围挡带来的变化
      if (vectorHeat && altRing && altRing.length > 2) {
        const alt = new BMapGL.Polyline(
          altRing.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
          { strokeColor: '#475569', strokeWeight: 1.6, strokeOpacity: 0.85, strokeStyle: 'dotted' },
        )
        alt.setZIndex?.(5)
        add(alt)
      }

      const c = isochrone.properties.center
      const extent = blindspots?.layout === 'disc' ? (blindspots.extent_m ?? 1500) : 0
      if (extent > 0 && c && (layers.blind || layers.heat)) {
        const dLat = extent / 111_320
        const dLng = extent / (111_320 * Math.cos((c.lat * Math.PI) / 180))
        viewPoints.push(
          new BMapGL.Point(c.lng - dLng, c.lat - dLat),
          new BMapGL.Point(c.lng + dLng, c.lat + dLat),
        )
      }

      if (layers.innerRings) {
        for (const inner of isochrone.properties.rings ?? []) {
          const innerPoints = inner.coordinates.map(([lng, lat]) => new BMapGL.Point(lng, lat))
          const layer = new BMapGL.Polygon(innerPoints, {
            strokeColor: '#1f7a42',
            strokeWeight: vectorHeat ? (inner.minutes <= 5 ? 1 : 1.25) : 0,
            strokeOpacity: vectorHeat ? 0.95 : 0,
            fillColor: '#2f9e5a',
            fillOpacity: (inner.minutes <= 5 ? 0.28 : 0.16) * fade,
            enableClicking: false,
          })
          layer.setZIndex?.(inner.minutes <= 5 ? 8 : 6)
          add(layer)
        }
      }

      // 设施标放在方格和等时圈之上。桃浦圈内设施为 0，点都在圈外，
      // 若压在盲区方格下面，打开样例就像一张空图。
      for (const place of isochrone.properties.coverage?.places ?? []) {
        if (place.fresh_status === 'excluded') continue
        if (!layers.outsidePlaces && !place.in_circle) continue
        add(
          placeMarker(BMapGL, place, dimPlaces, () => {
            const drawing = drawingRef.current
            if (drawing) {
              // 画「设施失效」：点设施标就是选中它；画别的类型：让这一下落到地图上放点
              if (drawing === 'facility_missing' && place.source !== 'user') {
                suppressPickRef.current = true
                pickPlaceRef.current?.(place)
              }
              return
            }
            if (pickEnabledRef.current) return
            suppressPickRef.current = true
            openPopover({ kind: 'place', place }, place)
          }),
        )
      }
    }

    // 两地对比：B 圈用橙色与主圈区分，并标注 B 的中心名称
    if (compare) {
      const ring = compare.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      add(
        new BMapGL.Polygon(points, {
          strokeColor: '#b0801f',
          strokeWeight: vectorHeat ? 1.5 : 0,
          strokeOpacity: vectorHeat ? 0.95 : 0,
          strokeStyle: 'dashed',
          fillColor: '#b0801f',
          fillOpacity: 0.08,
          enableClicking: false,
        }),
      )
      viewPoints.push(...points)
      const bCenter = compare.properties.center
      if (bCenter) {
        const bLabel = new BMapGL.Label(
          `B · ${escapeHtml((compare.properties.name ?? '').replace(/^上海市普陀区/, '') || '对比地点')}`,
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

    // 临时围挡：橙褐色虚线圆。尚未参与计算的同样画出，便于确认位置
    for (const closure of closures) {
      const point = new BMapGL.Point(closure.lng, closure.lat)
      const circle = new BMapGL.Circle(point, closure.radius_m, {
        strokeColor: '#9a3412',
        strokeWeight: 1.5,
        strokeOpacity: 0.9,
        strokeStyle: 'dashed',
        fillColor: '#ea580c',
        fillOpacity: 0.18,
        enableClicking: false,
      })
      circle.setZIndex?.(9)
      add(circle)
      const label = new BMapGL.Label('假设围挡', {
        position: point,
        offset: new BMapGL.Size(-26, -9),
      })
      label.setStyle({
        color: '#fff',
        background: '#9a3412',
        border: 'none',
        borderRadius: '8px',
        padding: '1px 6px',
        fontSize: '11px',
        fontWeight: '600',
      })
      label.setZIndex?.(95)
      add(label)
    }

    if (layers.clues) {
      // 复测巡检：有变化的方向画出新旧两条路线，一眼看出新路线在哪里绕开了原来的走法
      for (const ray of recheckRays) {
        if (ray.old_path && ray.old_path.length > 1) {
          const old = new BMapGL.Polyline(
            ray.old_path.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
            { strokeColor: '#6b7280', strokeWeight: 2, strokeOpacity: 0.8 * fade, strokeStyle: 'dashed' },
          )
          old.setZIndex?.(10)
          add(old)
        }
        if (ray.new_path && ray.new_path.length > 1) {
          const fresh = new BMapGL.Polyline(
            ray.new_path.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
            {
              strokeColor: ray.status === 'shorter' ? '#15803d' : SUSPECT_COLOR,
              strokeWeight: 3,
              strokeOpacity: 0.85 * fade,
            },
          )
          fresh.setZIndex?.(11)
          add(fresh)
        }
      }

      // 疑似新增阻断：紫色虚线圆 + 被放弃的旧路段，点开看原因。确认后才变成围挡
      for (const s of suspects) {
        const point = new BMapGL.Point(s.lng, s.lat)
        const circle = new BMapGL.Circle(point, s.radius_m, {
          strokeColor: SUSPECT_COLOR,
          strokeWeight: 2,
          strokeOpacity: 0.95 * fade,
          strokeStyle: 'dashed',
          fillColor: SUSPECT_COLOR,
          fillOpacity: 0.12 * fade,
          enableClicking: false,
        })
        circle.setZIndex?.(12)
        add(circle)
        if (s.segment.length > 1) {
          const seg = new BMapGL.Polyline(
            s.segment.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
            { strokeColor: '#dc2626', strokeWeight: 4, strokeOpacity: 0.7 * fade, strokeStyle: 'dashed' },
          )
          seg.setZIndex?.(13)
          add(seg)
        }
        const tag = new BMapGL.Label(`疑似 ${escapeHtml(s.id)}`, {
          position: point,
          offset: new BMapGL.Size(-22, -10),
        })
        tag.setStyle({
          color: '#fff',
          background: SUSPECT_COLOR,
          border: '1px solid rgba(255, 255, 255, 0.85)',
          borderRadius: '8px',
          padding: '1px 6px',
          fontSize: '11px',
          fontWeight: '600',
          cursor: 'pointer',
          opacity: String(fade),
        })
        tag.setZIndex?.(96)
        tag.addEventListener('click', () => {
          if (drawingRef.current || pickEnabledRef.current) return
          suppressPickRef.current = true
          openPopover({ kind: 'suspect', suspect: s }, s)
        })
        add(tag)
      }

      // 工地 POI 候选：琥珀色「工」字标
      for (const site of constructionSites) {
        const point = new BMapGL.Point(site.lng, site.lat)
        const mark = new BMapGL.Label('工', {
          position: point,
          offset: new BMapGL.Size(-11, -11),
        })
        mark.setStyle({
          color: '#fff',
          background: SITE_COLOR,
          border: site.on_route ? '2px solid #7c3aed' : '2px solid #fff7ed',
          borderRadius: '4px',
          width: '22px',
          height: '22px',
          lineHeight: '18px',
          textAlign: 'center',
          fontSize: '11px',
          fontWeight: '700',
          cursor: 'pointer',
          opacity: String(fade),
        })
        mark.setZIndex?.(94)
        mark.addEventListener('click', () => {
          if (drawingRef.current || pickEnabledRef.current) return
          suppressPickRef.current = true
          openPopover({ kind: 'site', site }, site)
        })
        add(mark)
      }
    }

    // 视野只在换了结果时贴合：切图层、切品类、切看法、画标注时保持用户当前的缩放与平移
    const key = fitKey ?? isochrone
    const viewChanged = viewKeyRef.current[0] !== key || viewKeyRef.current[1] !== compare
    if (viewChanged) {
      viewKeyRef.current = [key, compare]
      if (viewPoints.length > 0) {
        // 视野贴合圈范围，比固定缩放级别更实用：不同社区的可达范围差异很大
        map.setViewport(viewPoints)
      } else {
        map.setCenter(new BMapGL.Point(center.lng, center.lat))
      }
    }

    if (!tripOpen) add(new BMapGL.Marker(new BMapGL.Point(center.lng, center.lat)))

    // 六类均标拟建位置；有该品类网格判定时才显示 1 公里候选范围。
    if (simulation) {
      const simPoint = new BMapGL.Point(simulation.lng, simulation.lat)
      if (simulation.grid_evaluated !== false) {
        const reach = new BMapGL.Circle(simPoint, 1000, {
          strokeColor: '#c0391d',
          strokeWeight: 1.2,
          strokeOpacity: 0.55,
          strokeStyle: 'dashed',
          fillColor: '#c0391d',
          fillOpacity: 0.04,
          enableClicking: false,
        })
        add(reach)
      }
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
  }, [
    ready,
    center,
    isochrone,
    fitKey,
    layers,
    blindCategory,
    simulation,
    compare,
    closures,
    suspects,
    recheckRays,
    constructionSites,
    altRing,
    dimmed,
    dimPlaces,
    tripOpen,
    openPopover,
    vectorHeat,
    grayOutlines,
    guideActive,
  ])

  const rays = isochrone?.properties.rays
  const [radiusLo, radiusHi] = radiusLimits

  // 共享标注与草图单独成层：画草图时每点一下都要重画，不能连带重建上百个网格方格
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL) return
    markingOverlaysRef.current.forEach((o) => map.removeOverlay(o))
    markingOverlaysRef.current = []
    if (guideActive) return
    const add = (overlay: any) => {
      map.addOverlay(overlay)
      markingOverlaysRef.current.push(overlay)
    }
    const drawing = draft !== null

    const chip = (
      at: LatLng,
      text: string,
      style: Record<string, string>,
      z: number,
      onClick?: () => void,
      offset: [number, number] = [-12, -12],
    ) => {
      const label = new BMapGL.Label(text, {
        position: new BMapGL.Point(at.lng, at.lat),
        offset: new BMapGL.Size(offset[0], offset[1]),
      })
      label.setStyle({
        minWidth: '24px',
        height: '24px',
        padding: '0 4px',
        lineHeight: '20px',
        textAlign: 'center',
        fontSize: '11px',
        fontWeight: '700',
        borderRadius: '7px',
        whiteSpace: 'nowrap',
        boxShadow: '0 1px 4px rgba(0, 0, 0, 0.25)',
        cursor: onClick ? 'pointer' : 'default',
        ...style,
      })
      label.setZIndex?.(z)
      if (onClick) {
        label.addEventListener('click', () => {
          if (drawingRef.current) return
          suppressPickRef.current = true
          onClick()
        })
      }
      add(label)
      return label
    }

    if (layers.markings) {
      for (const m of markings) {
        if (tripOpen && (m.type !== 'closure' || !(m.status === 'verified' || m.mine || isochrone?.properties.markings?.applied.some(applied => applied.id === m.id)))) continue
        const meta = TYPE_META[m.type]
        const verified = m.status === 'verified'
        const selected = m.id === selectedMarkingId
        const select = () => selectMarkingRef.current?.(m.id)
        const fade = drawing ? DIM : 1
        const shapeStyle = {
          strokeColor: meta.color,
          strokeWeight: selected ? 3.5 : verified ? 2 : 1.6,
          strokeOpacity: 0.95 * fade,
          strokeStyle: verified ? 'solid' : 'dashed',
          fillColor: meta.color,
          fillOpacity: (selected ? 0.24 : verified ? 0.16 : 0.08) * fade,
          // 画草图时让点击穿过已有标注落到地图上
          enableClicking: !drawing,
        }
        const spec = m.spec
        if (m.type === 'closure' && spec.lat != null && spec.lng != null && spec.radius_m) {
          const circle = new BMapGL.Circle(new BMapGL.Point(spec.lng, spec.lat), spec.radius_m, shapeStyle)
          circle.setZIndex?.(selected ? 15 : 12)
          if (!drawing) {
            circle.addEventListener('click', () => {
              suppressPickRef.current = true
              select()
            })
          }
          add(circle)
        } else if (m.type === 'gray_area' && spec.polygon && spec.polygon.length >= 3) {
          const poly = new BMapGL.Polygon(
            spec.polygon.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
            shapeStyle,
          )
          poly.setZIndex?.(selected ? 15 : 11)
          if (!drawing) {
            poly.addEventListener('click', () => {
              suppressPickRef.current = true
              select()
            })
          }
          add(poly)
        }
        // 状态写在徽标上：已核实实底、待核实白底虚框、有争议加叹号、自己的加一圈描边
        const mark = `${meta.glyph}${verified ? '✓' : ''}${m.disputed ? '!' : ''}`
        chip(
          { lat: m.lat, lng: m.lng },
          mark,
          {
            color: verified ? '#fff' : meta.color,
            background: verified ? meta.color : '#fffdf8',
            border: `${selected ? 2.5 : 1.5}px ${verified ? 'solid' : 'dashed'} ${verified ? '#fff' : meta.color}`,
            outline: m.mine ? '2px solid #1f7a42' : 'none',
            outlineOffset: '1px',
            transform: selected ? 'scale(1.18)' : 'none',
            opacity: String(fade),
          },
          selected ? 99 : 97,
          select,
        )
      }
    }

    if (!draft) return
    const color = TYPE_META[draft.type].color
    const dot = (p: LatLng, first = false) => {
      const label = new BMapGL.Label('', {
        position: new BMapGL.Point(p.lng, p.lat),
        offset: new BMapGL.Size(first ? -7 : -5, first ? -7 : -5),
      })
      label.setStyle({
        width: first ? '14px' : '10px',
        height: first ? '14px' : '10px',
        padding: '0',
        background: '#fff',
        border: `2.5px solid ${color}`,
        borderRadius: '50%',
        boxShadow: '0 1px 3px rgba(0, 0, 0, 0.35)',
      })
      label.setZIndex?.(120)
      add(label)
    }

    if (draft.type === 'closure' && draft.point) {
      const centerPoint = draft.point
      // 被围挡挡住的步行路线标红，其余路线淡灰作底：提交前就看清它会影响哪几个方向
      const paths = (rays ?? []).filter((r) => (r.route_path?.length ?? 0) >= 2)
      let hits = 0
      for (const ray of paths) {
        const hit = circleHitsPath(centerPoint, draft.radius, ray.route_path!)
        if (hit) hits += 1
        const line = new BMapGL.Polyline(
          ray.route_path!.map(([lng, lat]) => new BMapGL.Point(lng, lat)),
          {
            strokeColor: hit ? '#dc2626' : '#94a3b8',
            strokeWeight: hit ? 3.2 : 1.4,
            strokeOpacity: hit ? 0.9 : 0.45,
            enableClicking: false,
          },
        )
        line.setZIndex?.(hit ? 116 : 114)
        add(line)
      }

      const circle = new BMapGL.Circle(new BMapGL.Point(centerPoint.lng, centerPoint.lat), draft.radius, {
        strokeColor: color,
        strokeWeight: DRAFT_STROKE,
        strokeOpacity: 1,
        strokeStyle: 'dashed',
        fillColor: color,
        fillOpacity: 0.2,
        enableClicking: false,
      })
      circle.setZIndex?.(118)
      add(circle)
      dot(centerPoint, true)

      if (paths.length > 0) {
        chip(
          centerPoint,
          hits > 0 ? `挡住 ${hits} 个方向` : '没挡住任何方向',
          {
            color: '#fff',
            background: hits > 0 ? '#dc2626' : '#64748b',
            border: '1.5px solid #fff',
            padding: '0 8px',
            borderRadius: '12px',
          },
          121,
          undefined,
          [-40, 16],
        )
      }

      // 圆边上的手柄：拖动时只改覆盖物本身，松手才写进草图（才进撤销栈）
      const edge = eastOf(centerPoint, draft.radius)
      const handle = new BMapGL.Marker(new BMapGL.Point(edge.lng, edge.lat), {
        icon: new BMapGL.Icon(handleIcon(color), new BMapGL.Size(20, 20), {
          anchor: new BMapGL.Size(10, 10),
        }),
        enableDragging: true,
        title: '拖动调整半径',
      })
      handle.enableDragging?.()
      handle.setZIndex?.(130)
      const tag = chip(
        edge,
        `${draft.radius} 米`,
        { color, background: '#fff', border: `1.5px solid ${color}`, padding: '0 6px' },
        131,
        undefined,
        [12, -30],
      )
      const radiusNow = () => {
        const pos = handle.getPosition()
        const r = Math.round(metersBetween(centerPoint, { lat: pos.lat, lng: pos.lng }) / 5) * 5
        return Math.min(radiusHi, Math.max(radiusLo, r))
      }
      handle.addEventListener('dragging', () => {
        const r = radiusNow()
        circle.setRadius?.(r)
        tag.setContent?.(`${r} 米`)
        tag.setPosition?.(handle.getPosition())
      })
      handle.addEventListener('dragend', () => {
        dragEndAtRef.current = Date.now()
        radiusRef.current?.(radiusNow())
      })
      handle.addEventListener('click', () => {
        dragEndAtRef.current = Date.now()
      })
      add(handle)
    } else if (draft.type === 'facility_extra' && draft.point) {
      chip(draft.point, '补', { color: '#fff', background: color, border: '2px solid #fff' }, 121)
    } else if (draft.type === 'facility_missing' && draft.point) {
      const ring = new BMapGL.Circle(new BMapGL.Point(draft.point.lng, draft.point.lat), 28, {
        strokeColor: color,
        strokeWeight: DRAFT_STROKE,
        strokeOpacity: 1,
        fillColor: color,
        fillOpacity: draft.place ? 0.18 : 0.06,
        strokeStyle: draft.place ? 'solid' : 'dashed',
        enableClicking: false,
      })
      ring.setZIndex?.(118)
      add(ring)
      if (!draft.place) dot(draft.point, true)
    } else if (draft.type === 'gray_area' && draft.vertices.length > 0) {
      const pts = draft.vertices.map((v) => new BMapGL.Point(v.lng, v.lat))
      if (pts.length >= 3) {
        const poly = new BMapGL.Polygon(pts, {
          strokeColor: color,
          strokeWeight: 0,
          fillColor: color,
          fillOpacity: 0.2,
          enableClicking: false,
        })
        poly.setZIndex?.(117)
        add(poly)
      }
      if (pts.length >= 2) {
        const line = new BMapGL.Polyline(pts, {
          strokeColor: color,
          strokeWeight: DRAFT_STROKE,
          strokeOpacity: 1,
          enableClicking: false,
        })
        line.setZIndex?.(118)
        add(line)
      }
      if (pts.length >= 3) {
        // 自动闭合的最后一段画成虚线：点下一个顶点时它会跟着移动
        const closing = new BMapGL.Polyline([pts[pts.length - 1], pts[0]], {
          strokeColor: color,
          strokeWeight: 2,
          strokeOpacity: 0.8,
          strokeStyle: 'dashed',
          enableClicking: false,
        })
        closing.setZIndex?.(118)
        add(closing)
      }
      draft.vertices.forEach((v, i) => dot(v, i === 0))
    }
  }, [ready, markings, selectedMarkingId, draft, layers.markings, rays, radiusLo, radiusHi, guideActive, tripOpen, isochrone])

  // 「在地图上看」：平移到标注处，放大到能看清街道
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL || !focus) return
    const zoom = typeof map.getZoom === 'function' ? map.getZoom() : 15
    map.centerAndZoom(new BMapGL.Point(focus.lng, focus.lat), Math.max(zoom, 16))
  }, [ready, focus])

  // 进入标注模式时，点进来的位置可能落在右侧抽屉或顶部模式条下面：平移到看得见的地方。
  // 等侧栏收起、地图重铺之后再算，否则用的是旧的容器尺寸
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL || !reveal) return
    const timer = window.setTimeout(() => {
      const el = shellRef.current
      if (!el) return
      const px = map.pointToPixel(new BMapGL.Point(reveal.lng, reveal.lat))
      const minX = 48
      const maxX = el.clientWidth - reveal.rightInset - 48
      const minY = 90
      const maxY = el.clientHeight - 48
      if (maxX <= minX || maxY <= minY) return
      const tx = Math.min(Math.max(px.x, minX), maxX)
      const ty = Math.min(Math.max(px.y, minY), maxY)
      if (tx === px.x && ty === px.y) return
      const c = map.pointToPixel(map.getCenter())
      map.panTo(map.pixelToPoint(new BMapGL.Pixel(c.x + (px.x - tx), c.y + (px.y - ty))))
    }, 180)
    return () => window.clearTimeout(timer)
  }, [ready, reveal])

  // 热力层：栅格放大 + 平滑插值，只画在实测过的网格范围内。
  // 现版网格只铺在 15 分钟圈内，色场裁到等时圈；若遇到圆形网格的结果则裁到那个圆。
  // 它是独立画布而非地图覆盖物——覆盖物只能画矢量，铺不出连续色场。
  useEffect(() => {
    const map = mapRef.current
    const canvas = heatRef.current
    const BMapGL = window.BMapGL
    if (!map || !canvas || !BMapGL) return

    const blindspots = isochrone?.properties.blindspots
    const tile =
      layers.heat && blindspots && !guideActive && !tripOpen
        ? buildHeatTile(
            blindspots.cells,
            blindspots.grid_spacing_m,
            heatScaleS(blindspots.max_reach_s, isochrone?.properties.minutes),
            0.32,
          )
        : null
    const ring = isochrone?.geometry.coordinates[0] ?? []
    const gridCenter = isochrone?.properties.center
    const discRadiusM =
      blindspots?.layout === 'disc'
        ? (blindspots.extent_m ?? 1500) + blindspots.grid_spacing_m / 2
        : 0

    const paint = () => {
      const width = canvas.clientWidth
      const height = canvas.clientHeight
      const dpr = window.devicePixelRatio || 1
      if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
        canvas.width = Math.round(width * dpr)
        canvas.height = Math.round(height * dpr)
      }
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)
      // 倾斜、旋转后整张图不再是正放的矩形，画布色场会错位：这时热力由矢量方格画，画布留空
      if (vectorHeat || guideActive || tripOpen) return
      if (tile && (ring.length > 2 || (discRadiusM > 0 && gridCenter))) {
        ctx.save()
        ctx.beginPath()
        if (discRadiusM > 0 && gridCenter) {
          const c = map.pointToPixel(new BMapGL.Point(gridCenter.lng, gridCenter.lat))
          const n = northOf(gridCenter, discRadiusM)
          const edge = map.pointToPixel(new BMapGL.Point(n.lng, n.lat))
          ctx.arc(c.x, c.y, Math.hypot(edge.x - c.x, edge.y - c.y), 0, Math.PI * 2)
        } else {
          ring.forEach(([lng, lat], i) => {
            const p = map.pointToPixel(new BMapGL.Point(lng, lat))
            if (i === 0) ctx.moveTo(p.x, p.y)
            else ctx.lineTo(p.x, p.y)
          })
          ctx.closePath()
        }
        ctx.clip()
        const nw = map.pointToPixel(new BMapGL.Point(tile.west, tile.north))
        const se = map.pointToPixel(new BMapGL.Point(tile.east, tile.south))
        ctx.imageSmoothingEnabled = true
        ctx.imageSmoothingQuality = 'high'
        ctx.drawImage(tile.image, nw.x, nw.y, se.x - nw.x, se.y - nw.y)
        ctx.restore()
      }
    }

    paint()
    const events = ['moving', 'moveend', 'zooming', 'zoomend', 'resize']
    events.forEach((e) => map.addEventListener(e, paint))
    return () => events.forEach((e) => map.removeEventListener(e, paint))
  }, [ready, isochrone, layers.heat, shell, vectorHeat, guideActive, tripOpen])

  // 卡片跟着锚点走：地图平移、缩放时重算像素位置
  const popoverAt = popover?.at
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL || !popoverAt) return
    let frame = 0
    const update = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const px = map.pointToPixel(new BMapGL.Point(popoverAt.lng, popoverAt.lat))
        setPopover((p) => (p && p.at === popoverAt ? { ...p, x: px.x, y: px.y } : p))
      })
    }
    // 3D 下旋转、倾斜也会让锚点在屏幕上移动
    const events = [
      'moving',
      'moveend',
      'zooming',
      'zoomend',
      'resize',
      'tilt_changed',
      'heading_changed',
    ]
    events.forEach((e) => map.addEventListener(e, update))
    return () => {
      cancelAnimationFrame(frame)
      events.forEach((e) => map.removeEventListener(e, update))
    }
  }, [ready, popoverAt])

  // 结果换了、进了标注模式：卡片不再显示（判定内容已经不是它说的那份）
  const shownPopover = popover && popover.owner === isochrone && !draft && !guideActive && !tripOpen ? popover : null
  const shownId = shownPopover?.id ?? null
  useEffect(() => {
    popoverOpenRef.current = shownId !== null
    if (shownId === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPopover(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [shownId])

  const blindspots = isochrone?.properties.blindspots
  const labeledRegions = (isochrone?.properties.report?.gray_regions?.regions ?? []).filter(
    (r) => r.id,
  ).length
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    const BMapGL = (window as any).BMapGL
    const clear = () => { tripOverlaysRef.current.forEach(overlay => map.removeOverlay(overlay)); tripOverlaysRef.current = [] }
    const add = (overlay: any, zIndex: number) => { overlay.setZIndex?.(zIndex); map.addOverlay(overlay); tripOverlaysRef.current.push(overlay) }
    const draw = () => {
      clear()
      if (!trip) return
      const highlight = trip.hover ?? trip.selected
      const line = (path: [number, number][], color: string, width: number, opacity: number, dashed: boolean, z: number, id?: string) => {
        if (path.length < 2) return
        const overlay = new BMapGL.Polyline(path.map(([lng, lat]) => new BMapGL.Point(lng, lat)), { strokeColor: color, strokeWeight: width, strokeOpacity: opacity, strokeStyle: dashed ? 'dashed' : 'solid', enableClicking: Boolean(id) })
        if (id) overlay.addEventListener('click', () => { if (pickEnabledRef.current || drawingRef.current) return; suppressPickRef.current = true; onTripSelect?.(id) })
        add(overlay, z)
      }
      const label = (at: LatLng, glyph: string, color: string, selected: boolean) => {
        const overlay = new BMapGL.Label(escapeHtml(glyph), { position: new BMapGL.Point(at.lng, at.lat), offset: new BMapGL.Size(-12, -12) })
        overlay.setStyle({ background: color, color: '#fff', padding: '4px 7px', border: selected ? '2px solid white' : '1px solid white', borderRadius: '5px', fontSize: '13px', fontWeight: '700' })
        add(overlay, 98)
      }
      if (guide) {
        const item = guide.item
        if (vectorHeat && item.route) {
          line(item.route.path, '#fff', 7.5, .95, false, 15)
          line(item.route.path, '#1f7a42', 4.5, 1, false, 16)
          const stepPath = item.route.steps[guide.stepIndex]?.path
          if (stepPath) line(stepPath, '#1677d2', 5, 1, false, 17)
          for (const connector of item.route.connectors) line(connector, '#6b7280', 2, .8, true, 18)
        }
        label(item.from, '起', '#1c3a28', true)
        label(item, '终', '#1f7a42', true)
        if (guide.location) label(guide.location, Date.now() - guide.location.timestamp > 30000 ? '上次定位' : '● 我', '#1677d2', true)
        return
      }
      const items = [...trip.items].sort((a, b) => Number(a.entry_id === highlight) - Number(b.entry_id === highlight))
      for (const item of items) {
        const selected = item.entry_id === highlight
        const path = trip.itinerary ? itineraryPath(item, trip.preview) : trip.preview ? [[item.from.lng, item.from.lat], [item.lng, item.lat]] as [number, number][] : tripPath(item)
        const dashed = trip.preview || !item.route
        if (trip.itinerary) {
          const style = itineraryLineStyle(item, selected, Boolean(highlight), trip.preview, trip.items.indexOf(item))
          if (vectorHeat) {
            line(path, '#fff', style.weight + 3, style.alpha, style.dashed, selected ? 15 : 12)
            line(path, style.color, style.weight, style.alpha, style.dashed, selected ? 16 : 13, item.entry_id)
            for (const connector of item.route?.connectors ?? []) line(connector, '#6b7280', 2, .8, true, 16)
            if (item.route?.blocked_path.length) line(item.route.blocked_path, '#c0392b', 4, 1, true, 17, item.entry_id)
          }
          continue
        }
        if (selected) line(path, '#fff', 7, .95, dashed, 15)
        line(path, tripColor(item.category), selected ? 4 : 3, selected ? 1 : trip.itinerary ? .7 : .45, dashed, selected ? 16 : 14, item.entry_id)
        for (const connector of item.route?.connectors ?? []) line(connector, '#6b7280', 2, .8, true, 16)
        if (item.route?.blocked_path.length) line(item.route.blocked_path, '#dc2626', 4, 1, true, 17, item.entry_id)
        label(item, String(item.rank), tripColor(item.category), selected)
      }
      if (!trip.itinerary) label(trip.origin, '起', '#1c3a28', true)
    }
    draw()
    window.addEventListener('beforeprint', clear)
    window.addEventListener('afterprint', draw)
    return () => { clear(); window.removeEventListener('beforeprint', clear); window.removeEventListener('afterprint', draw) }
  }, [ready, trip, onTripSelect, guide, vectorHeat])

  useEffect(() => {
    if (!trip) { tripFitRef.current = null; return }
    const map = mapRef.current, BMapGL = window.BMapGL
    if (!ready || !map || !BMapGL || guide) return
    // Leaving immersion restores its saved camera; consume its selection without overriding that restoration.
    if (guideViewRef.current) { tripFitRef.current = trip; return }
    const request = tripCameraRequest(tripFitRef.current, trip)
    if (!request) return
    const bounds = tripViewBounds(trip, request.selected)
    if (!bounds.length) return
    return afterStableMapSize(() => ({ w: shellRef.current?.clientWidth ?? 0, h: shellRef.current?.clientHeight ?? 0 }), () => {
      const el = shellRef.current
      if (!el) return
      const stage = el.closest('.stage'), box = el.getBoundingClientRect()
      const drawer = stage?.querySelector('.trip-drawer:not([hidden])')?.getBoundingClientRect()
      const toolbar = Array.from(stage?.querySelectorAll('.map-search, .map-tools') ?? []).map(node => node.getBoundingClientRect()).sort((a, b) => b.bottom - a.bottom)[0]
      const margins = tripViewportMargins(box, drawer, toolbar)
      const points = bounds.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      if (typeof map.getViewport === 'function') {
        const viewport = map.getViewport(points, { margins })
        const cap = typeof map.getMaxZoom === 'function' ? Number(map.getMaxZoom()) || 20 : 20
        map.centerAndZoom(viewport.center, Math.min(cap, request.selected ? 19 : 18, viewport.zoom))
      } else map.setViewport(points, { margins, enableAnimation: false })
      tripFitRef.current = trip
    })
  }, [ready, trip, guide, shell.w, shell.h])

  // 出行只是临时工作区：图层开关不变，关闭后恢复体检时的视角。
  useEffect(() => {
    const map = mapRef.current
    if (!ready || !map) return
    if (tripOpen) {
      if (!tripViewRef.current) tripViewRef.current = { owner: isochrone, center: map.getCenter(), zoom: map.getZoom(), tilt: Number(map.getTilt?.()) || 0, heading: Number(map.getHeading?.()) || 0, earth: sceneEarth }
      return
    }
    const previous = tripViewRef.current
    if (!previous) return
    if (previous.owner !== isochrone) { tripViewRef.current = null; return }
    return afterStableMapSize(() => ({ w: shellRef.current?.clientWidth ?? 0, h: shellRef.current?.clientHeight ?? 0 }), () => {
      setSceneEarth(previous.earth)
      map.centerAndZoom(previous.center, previous.zoom)
      map.setTilt?.(previous.tilt); map.setHeading?.(previous.heading)
      lastCenterRef.current = previous.center; tripViewRef.current = null
    })
  }, [ready, tripOpen, isochrone, sceneEarth, setSceneEarth, shell.w, shell.h])

  // 进入时保存视野；退出时在抽屉、侧栏恢复尺寸后还原，不改变原查询结果。
  useEffect(() => {
    const map = mapRef.current, BMapGL = window.BMapGL
    if (!ready || !map || !BMapGL) return
    if (!guide) {
      const previous = guideViewRef.current
      if (!previous) return
      guideFocusRef.current = null
      if (!tripOpen) { guideViewRef.current = null; return }
      if (previous.owner !== isochrone) { guideViewRef.current = null; return }
      const restore = () => {
        setSceneEarth(previous.earth)
        map.centerAndZoom(previous.center, previous.zoom)
        map.setTilt?.(previous.tilt); map.setHeading?.(previous.heading)
        lastCenterRef.current = previous.center
        guideViewRef.current = null
      }
      return afterStableMapSize(() => ({ w: shellRef.current?.clientWidth ?? 0, h: shellRef.current?.clientHeight ?? 0 }), restore)
    }
    if (!guideViewRef.current) {
      guideViewRef.current = { owner: isochrone, center: map.getCenter(), zoom: map.getZoom(), tilt: Number(map.getTilt?.()) || 0, heading: Number(map.getHeading?.()) || 0, earth: sceneEarth }
      map.setTilt?.(0); map.setHeading?.(0)
    }
    return afterStableMapSize(() => ({ w: shellRef.current?.clientWidth ?? 0, h: shellRef.current?.clientHeight ?? 0 }), () => {
      const newFocus = guideFocusRef.current !== guide.focusKey
      const point = (p: LatLng) => new BMapGL.Point(p.lng, p.lat)
      const el = shellRef.current
      if (!el) return
      const box = el.getBoundingClientRect(), stage = el.closest('.stage')
      const head = stage?.querySelector('.guide-map-actions')?.getBoundingClientRect()
      const card = stage?.querySelector('.guide-step-card')?.getBoundingClientRect()
      const top = Math.min(box.height * .35, (head?.bottom ?? box.top + 150) - box.top + 28)
      const bottom = Math.min(box.height * .4, card ? box.bottom - card.top + 16 : 80)
      const margins = [top, 55, bottom, 55]
      const fit = (path: [number, number][], local: boolean) => {
        const points = path.map(([lng, lat]) => new BMapGL.Point(lng, lat))
        if (local && typeof map.getViewport === 'function') {
          const viewport = map.getViewport(points, { margins })
          const cap = typeof map.getMaxZoom === 'function' ? Number(map.getMaxZoom()) || 20 : 20
          map.centerAndZoom(viewport.center, Math.min(cap, Math.max(18, Math.min(20, viewport.zoom))))
        } else map.setViewport(points, { margins, enableAnimation: false })
      }
      if (guide.focus === 'location' && guide.location) {
        if (newFocus) map.centerAndZoom(point(guide.location), 19)
        else map.panTo(point(guide.location))
      } else if (newFocus) {
        const path = guide.focus === 'step' ? guide.item.route?.steps[guide.stepIndex]?.path : guide.focus === 'route' ? guide.item.route?.path : null
        if (path && path.length > 1) fit(path, guide.focus === 'step')
        else fit(originViewBounds(guide.item), true)
      }
      guideFocusRef.current = guide.focusKey
    })
  }, [ready, guide, isochrone, sceneEarth, setSceneEarth, shell.w, shell.h, tripOpen])

  const showLegend = draft ? drawLegend : legendOpen

  return (
    <div ref={shellRef} className={draft ? 'map-shell drawing' : tripOpen ? 'map-shell trip-active' : 'map-shell'}>
      <div ref={containerRef} className="map-canvas" />
      <canvas ref={heatRef} className="heat-canvas" />
      <MapOutlines
        mapRef={mapRef}
        ready={ready}
        active={!vectorHeat && (guideActive || !tripOpen)}
        outlines={guideActive ? guideOutlines : mapOutlines}
        opacity={guideActive ? 1 : dimmed ? DIM : 1}
      />
      {!guide && trip?.itinerary && <>
        <MapOutlines mapRef={mapRef} ready={ready} active outlines={itineraryOutlines} opacity={1} arrowsOnly={vectorHeat} />
        <TripMapStops mapRef={mapRef} ready={ready} trip={trip} interactive={!pickEnabled} onSelect={onTripSelect} />
        {!trip.editing && trip.items.length > 0 && <div className="trip-map-segments" role="group" aria-label="地图显示的行程路段"><button type="button" aria-pressed={!trip.selected} onClick={() => onTripSelect?.(null)}>全程</button>{trip.items.map((item, i) => <button type="button" key={item.entry_id} style={{ '--leg-color': tripSegmentColor(i) } as React.CSSProperties} aria-pressed={trip.selected === item.entry_id} onClick={() => onTripSelect?.(item.entry_id)}><span aria-hidden="true" />{i ? i : '起点'} → {i + 1}</button>)}</div>}
      </>}
      {pickEnabled && !draft && !tripOpen && (
        <div className="map-mode floating live">
          <i />
          {pickHint ?? '点击地图将按新中心点重新计算（消耗配额）'}
        </div>
      )}
      {!draft && !guideActive && !tripOpen && (
        <MapViewBar
          layers={layers}
          onLayers={onLayers}
          available={{
            innerRings: (isochrone?.properties.rings?.length ?? 0) > 0,
            grid: (blindspots?.cells.length ?? 0) > 0,
            regions: labeledRegions,
            markings: markings.length,
            clues: suspects.length + constructionSites.length + recheckRays.length,
          }}
          blindCategory={blindCategory}
          blindCategories={blindCategories}
          onBlindCategory={(name) => onBlindCategory?.(name)}
          viewSwitch={viewSwitch}
          resultView={resultView}
          onResultView={onResultView}
          onRerun={onRerun}
          busy={busy}
        />
      )}
      {!draft && ready && <MapSceneBar scene={scene} immersive={guideActive} />}
      {!guideActive && !tripOpen && <button
        type="button"
        className="legend-toggle floating"
        aria-expanded={showLegend}
        onClick={() => (draft ? setDrawLegend((v) => !v) : setLegendOpen((v) => !v))}
      >
        图例
      </button>}
      {!guideActive && !tripOpen && showLegend && (
        <MapLegend
          isochrone={isochrone}
          layers={layers}
          blindCategory={blindCategory}
          simulation={simulation}
          closures={closures.length}
          suspects={suspects.length}
          recheckRays={recheckRays.length}
          sites={constructionSites.length}
          markings={markings.length}
          hasAltRing={Boolean(altRing && altRing.length > 2)}
          resultView={resultView}
          vectorHeat={vectorHeat}
          tripShown={Boolean(trip?.items.length)}
        />
      )}
      {shownPopover && shell.w > 0 && (
        <MapPopover
          key={shownPopover.id}
          ctx={shownPopover.ctx}
          x={shownPopover.x}
          y={shownPopover.y}
          shellW={shell.w}
          shellH={shell.h}
          canMark={canMark}
          feedbackDisabled={Boolean(isochrone?.properties.simulated)}
          canTrip={!draft && !pickEnabled && Boolean(isochrone?.properties.coverage?.places?.length || isochrone?.properties.simulated)}
          limitM={blindspots?.walk_limit_m ?? 1000}
          places={isochrone?.properties.coverage?.places ?? []}
          onIntent={(intent) => onIntent?.(intent)}
          onClose={() => setPopover(null)}
        />
      )}
    </div>
  )
}

/** 设施标。点击交给 onClick：弹卡片，或在画草图时选中 / 放点。 */
function placeMarker(BMapGL: any, place: Place, dim: boolean, onClick: () => void) {
  const mark = PLACE_MARK[place.category] ?? { glyph: '·', color: '#4b4e45' }
  const user = place.source === 'user'
  const pending = place.fresh_status === 'pending'
  const label = new BMapGL.Label(pending ? `${mark.glyph}?` : mark.glyph, {
    position: new BMapGL.Point(place.lng, place.lat),
    offset: new BMapGL.Size(-11, -11),
  })
  const base = place.in_circle ? 1 : 0.82
  label.setStyle({
    color: pending ? '#826020' : '#fff',
    background: pending ? '#f5edd8' : mark.color,
    // 用户补录并参与了本次计算的设施：青绿虚线描边，和地图检索到的区分开
    border: pending ? '2px dashed #b68b43' : user ? '2px dashed #0f766e' : '2px solid #f3faf4',
    borderRadius: '50%',
    width: '22px',
    height: '22px',
    lineHeight: '18px',
    textAlign: 'center',
    fontSize: '11px',
    fontWeight: '700',
    opacity: String(dim ? base * DIM : base),
    cursor: 'pointer',
    boxShadow: user ? '0 0 0 2px #ecfdf5' : 'none',
  })
  label.setZIndex?.(90)
  label.addEventListener('click', onClick)
  return label
}
