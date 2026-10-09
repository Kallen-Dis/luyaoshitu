import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import './App.css'
import './components/markings/markings.css'
import {
  ApiError,
  computeIsochroneStream,
  constructionCandidates,
  crosscheckFeature,
  fetchConfig,
  fetchDemo,
  fetchHistories,
  fetchHistory,
  fetchMarkings,
  fetchSample,
  fetchSamples,
  geocode,
  recheck,
  retractMarking,
  simulate,
  simulateClosures,
  sitePlan,
} from './api'
import { gridCensus } from './lib/grid'
import { CompareCard } from './components/CompareCard'
import { DirectionRadar } from './components/DirectionRadar'
import { ExportCard } from './components/ExportCard'
import { MapView } from './components/MapView'
import { TripDrawer } from './components/trip/TripDrawer'
import { TripGuide } from './components/trip/TripGuide'
import { PlanningDrawer } from './components/planning/PlanningDrawer'
import { FacilitySimulation } from './components/planning/FacilitySimulation'
import { guideUnavailableReason } from './lib/guide'
import { guideItinerary } from './lib/itinerary'
import {
  DEFAULT_LAYERS,
  type ComposerPreset,
  type LayerState,
  type MapIntent,
} from './components/map/context'
import { algorithmView, viewSwitch, type ResultView } from './lib/resultView'
import { NarrativeCard } from './components/NarrativeCard'
import { ReportCard } from './components/ReportCard'
import { SignatureCard } from './components/SignatureCard'
import { AdminReview } from './components/markings/AdminReview'
import { applyMapClick, emptyDraft, useDraftHistory } from './components/markings/draft'
import { Icon } from './components/markings/Icon'
import { MarkingComposer, type ComposerStep } from './components/markings/MarkingComposer'
import { MarkingEffectCard } from './components/markings/MarkingEffectCard'
import { MarkingPanel } from './components/markings/MarkingPanel'
import { UndoToast, type ToastSpec } from './components/markings/UndoToast'
import type {
  AppConfig,
  ClosureSpec,
  ConstructionResult,
  CrosscheckResult,
  CrosscheckSuspect,
  HistoryMeta,
  IsochroneFeature,
  Marking,
  MarkingType,
  Prescription,
  RecheckResult,
  SampleMeta,
  SimulationResult,
  SitePlanResult,
  TripOrigin,
  TripMapData,
  TripGuideData,
  TripItem,
  Place,
} from './types'

/** 某份结果专属的派生数据：owner 不是当前结果时视为不存在。 */
interface Scoped<T> {
  owner: IsochroneFeature | null
  value: T
}

function scoped<T>(entry: Scoped<T> | null, owner: IsochroneFeature | null): T | null {
  return entry && entry.owner === owner ? entry.value : null
}

const NO_KEYS: string[] = []
const NO_MARKINGS: Marking[] = []

function shortName(name: string) {
  return name.replace(/^上海市普陀区/, '')
}

function metersBetween(a: { lat: number; lng: number }, b: { lat: number; lng: number }) {
  const r = 6_371_000
  const p1 = (a.lat * Math.PI) / 180
  const p2 = (b.lat * Math.PI) / 180
  const dp = ((b.lat - a.lat) * Math.PI) / 180
  const dl = ((b.lng - a.lng) * Math.PI) / 180
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2
  return 2 * r * Math.asin(Math.min(1, Math.sqrt(h)))
}

/** 右栏第一眼只留分数和三句话。方向雷达在下面，不再并列五项数字。 */
function Verdict({ feature }: { feature: IsochroneFeature }) {
  const props = feature.properties
  const report = props.report
  if (!report) return null
  const inCount = report.categories
    ? Object.values(report.categories).reduce((acc, n) => acc + n, 0)
    : null
  const nearby = props.coverage?.nearby_categories
  const nearbyTotal = nearby ? Object.values(nearby).reduce((acc, n) => acc + n, 0) : 0
  const places = props.coverage?.places ?? []
  const nearest =
    props.center && places.length > 0
      ? places.reduce((best, place) => {
          const meters = metersBetween(props.center!, place)
          return meters < best.meters ? { name: place.name, meters } : best
        }, { name: places[0].name, meters: metersBetween(props.center, places[0]) })
      : null
  const reach =
    props.min_radius_m && props.max_radius_m
      ? `大约 ${(props.min_radius_m / 1000).toFixed(1)}–${(props.max_radius_m / 1000).toFixed(1)} 公里`
      : `大约 ${props.area_km2.toFixed(2)} km²`
  const minutes = props.minutes ?? 15
  // 数据来源放在第一眼：快照、实时、离线模拟的可信度差别很大，完整图签在侧栏最末
  const source = props.simulated
    ? '离线模拟（非真实路网）'
    : props.history_id == null && props.generated_at
      ? '预生成快照'
      : '实时计算'
  const generatedAt = props.generated_at
    ? new Date(props.generated_at).toLocaleString('zh-CN', {
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      })
    : null
  const sourceLine = [source, generatedAt, `${props.mode_label ?? '步行'} ${minutes} 分钟`]
    .filter(Boolean)
    .join(' · ')
  const missing =
    report.blinds.length > 0 && nearbyTotal > 0
      ? `从这个落点步行 ${minutes} 分钟走不到${report.blinds.join('、')}。周围 ${((props.coverage?.radius_m ?? 0) / 1000).toFixed(1)} 公里内有 ${nearbyTotal} 处${nearest ? `，最近的「${nearest.name}」直线 ${Math.round(nearest.meters)} 米，仍在圈外` : ''}。`
      : report.blinds.length > 0
        ? `从这个落点步行 ${minutes} 分钟走不到${report.blinds.join('、')}。`
        : `圈内 ${inCount ?? 0} 处民生设施，六个品类都找得到。`
  const census = gridCensus(props.blindspots)
  const delay = props.delay
  const extentKm = ((props.blindspots?.extent_m ?? 1500) / 1000).toFixed(1)
  const spacingM = Math.round(props.blindspots?.grid_spacing_m ?? 100)
  const lines = [
    `图上是「${shortName(props.name ?? '当前地点')}」地名落点的 ${minutes} 分钟步行范围，${reach}，面积 ${props.area_km2.toFixed(2)} km²。`,
    ...(delay?.applied
      ? [
          `已按步行路线补回过街与路口等待（每公里约 ${Math.round(delay.delay_per_km_s ?? 0)} 秒），圈面积由 ${delay.raw_area_km2.toFixed(2)} 缩到 ${delay.area_km2.toFixed(2)} km²。`,
        ]
      : []),
    missing,
    census
      ? props.blindspots?.layout === 'disc'
        ? `周围 ${extentKm} 公里 ${census.total} 个方格里，${census.blind} 格步行 1 公里到不了菜场、药店或小学（逐格实测路网，圈内 ${census.blindInCircle} / ${census.inCircle} 格）。`
        : `15 分钟圈内 ${census.total} 个 ${spacingM} 米方格里，${census.blind} 格步行 1 公里到不了菜场、药店或小学（逐格实测路网）。`
      : '还没有做网格盲区判定。',
  ]
  return (
    <section className="card verdict">
      <p className="verdict-place">{shortName(props.name ?? '当前地点')}</p>
      <p className={props.simulated ? 'verdict-source simulated' : 'verdict-source'}>
        {sourceLine}
      </p>
      <div className="verdict-score">
        <span className={`grade-${report.grade}`}>{report.total.toFixed(0)}</span>
        <em>/ 100</em>
        <b className={`grade-seal grade-${report.grade}`}>{report.grade}</b>
      </div>
      <p className="verdict-gloss">
        分数把路网能走多远、圈有没有被切开、绕行、圈内设施，和地图上的方格合在一起。
      </p>
      <ol>
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ol>
      {props.rays && props.rays.length >= 3 && (
        <div className="verdict-shape">
          <DirectionRadar rays={props.rays} />
        </div>
      )}
    </section>
  )
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [samples, setSamples] = useState<SampleMeta[]>([])
  const [histories, setHistories] = useState<HistoryMeta[]>([])
  const [activeSampleId, setActiveSampleId] = useState<string | null>(null)
  const [center, setCenter] = useState({ lat: 31.247979, lng: 121.416775 })
  const [isochrone, setIsochrone] = useState<IsochroneFeature | null>(null)
  const [minutes, setMinutes] = useState(15)
  const [directions, setDirections] = useState(36)
  const [address, setAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [stageText, setStageText] = useState<string | null>(null)
  const [stageId, setStageId] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [layers, setLayers] = useState<LayerState>(DEFAULT_LAYERS)
  const [withCoverage, setWithCoverage] = useState(true)
  const [pickEnabled, setPickEnabled] = useState(false)
  const [mode, setMode] = useState('walk')
  const [coordSys, setCoordSys] = useState('bd09')
  const [dataMode, setDataMode] = useState<'real' | 'demo'>('real')
  const [simulation, setSimulation] = useState<SimulationResult | null>(null)
  const [tripEntry, setTripEntry] = useState<Scoped<{ key: number; origin: TripOrigin; category?: string; targetId?: string }> | null>(null)
  const [tripMapEntry, setTripMapEntry] = useState<Scoped<TripMapData> | null>(null)
  const [tripPlacesEntry, setTripPlacesEntry] = useState<Scoped<Place[]> | null>(null)
  const [tripPicking, setTripPicking] = useState(false)
  const [tripSelection, setTripSelection] = useState<string | null>(null)
  const [tripSelectionKey, setTripSelectionKey] = useState(0)
  const selectTrip = useCallback((id: string | null) => { setTripSelection(id); setTripSelectionKey(key => key + 1) }, [])
  const [guideEntry, setGuideEntry] = useState<Scoped<TripGuideData> | null>(null)
  const closeGuide = useCallback(() => setGuideEntry(null), [])
  const closeTrip = useCallback(() => { setGuideEntry(null); setTripEntry(null); setTripMapEntry(null); setTripPicking(false); setTripSelection(null) }, [])
  const [compareFeature, setCompareFeature] = useState<IsochroneFeature | null>(null)
  const [compareId, setCompareId] = useState<string | null>(null)
  const [comparePicking, setComparePicking] = useState(false)
  const [placing, setPlacing] = useState<string | null>(null)
  const [planningMode, setPlanningMode] = useState<'facility' | 'closure' | null>(null)
  const [closurePreview, setClosurePreview] = useState<IsochroneFeature | null>(null)
  const [planningBusy, setPlanningBusy] = useState(false)
  const planningController = useRef<AbortController | null>(null)
  const planningRequest = useRef(0)
  const [samplesOpen, setSamplesOpen] = useState(false)
  const [latText, setLatText] = useState('31.247979')
  const [lngText, setLngText] = useState('121.416775')
  const [blindCategory, setBlindCategory] = useState('all')
  // 施工围挡：在地图上点选标注，下次实时计算时随请求提交
  const [closures, setClosures] = useState<ClosureSpec[]>([])
  const [closurePlacing, setClosurePlacing] = useState(false)
  const [closureRadius, setClosureRadius] = useState(50)
  const [crossingDelay, setCrossingDelay] = useState(true)
  // 下面几项都是针对「当前这份结果」算的：存的时候记下是哪份结果，
  // 结果一换（新计算、切样例、载入历史）读出来就是空，不必在 effect 里逐个清空
  // 围挡的自动线索：复测巡检（路线变长）与工地 POI。都只是候选，确认后才变成围挡
  const [recheckEntry, setRecheckEntry] = useState<Scoped<RecheckResult> | null>(null)
  const [constructionEntry, setConstructionEntry] = useState<Scoped<ConstructionResult> | null>(
    null,
  )
  const [closureBusy, setClosureBusy] = useState<'recheck' | 'poi' | null>(null)
  const [dismissedEntry, setDismissedEntry] = useState<Scoped<string[]> | null>(null)
  // 诊疗的两项增强：核验选址结果与 AI 二次核对（Agent Plan 再找一遍缺口附近的设施）
  const [sitePlanEntry, setSitePlanEntry] = useState<Scoped<SitePlanResult> | null>(null)
  const [sitePlanBusy, setSitePlanBusy] = useState<string | null>(null)
  const [crosscheckEntry, setCrosscheckEntry] = useState<Scoped<CrosscheckResult> | null>(null)
  const [crosscheckBusy, setCrosscheckBusy] = useState(false)
  const recheckResult = scoped(recheckEntry, isochrone)
  const constructionResult = scoped(constructionEntry, isochrone)
  const dismissed = scoped(dismissedEntry, isochrone) ?? NO_KEYS
  const sitePlanResult = scoped(sitePlanEntry, isochrone)
  const crosscheckResult = scoped(crosscheckEntry, isochrone)
  const dismiss = (key: string) => setDismissedEntry({ owner: isochrone, value: [...dismissed, key] })
  const currentRef = useRef(isochrone)
  currentRef.current = isochrone

  // ---------- 共享标注 ----------
  // 附近标注按「查询中心 + 版本号」拉取；每次新建、修改、投票、审核后版本号加一，列表与地图随之刷新
  const [nearbyEntry, setNearbyEntry] = useState<{ key: string; items: Marking[] } | null>(null)
  const [markingsError, setMarkingsError] = useState<string | null>(null)
  const [markingRevision, setMarkingRevision] = useState(0)
  const [selectedMarkingId, setSelectedMarkingId] = useState<number | null>(null)
  // 新建面板：key 变化即一个全新的面板（内部状态清零）。开着它就是「标注模式」
  const [composer, setComposer] = useState<{
    key: number
    source: 'user' | 'recheck' | 'poi' | 'agent_plan'
    step: ComposerStep
    preset: ComposerPreset | null
  } | null>(null)
  // 标注模式下侧栏收成窄条，把地图让出来；用户点开窄条可以临时展开
  const [railOpen, setRailOpen] = useState(false)
  const sidebarRef = useRef<HTMLElement>(null)
  const sidebarScrollRef = useRef(0)
  const [reveal, setReveal] = useState<{
    lat: number
    lng: number
    key: number
    rightInset: number
  } | null>(null)
  // 地图看哪种结果：「含标注」是页面上的结果，「纯算法」由差异还原。换了结果就回到「含标注」
  const [viewEntry, setViewEntry] = useState<Scoped<ResultView> | null>(null)
  const draftHistory = useDraftHistory(emptyDraft('closure'))
  const [toast, setToast] = useState<ToastSpec | null>(null)
  // 分析时怎么用附近的标注：auto = 已核实 + 自己的 + 采纳的；none = 纯算法
  const [markingMode, setMarkingMode] = useState<'auto' | 'none'>('auto')
  const [adopted, setAdopted] = useState<number[]>([])
  const [adminOpen, setAdminOpen] = useState(false)
  const [mapFocus, setMapFocus] = useState<{ lat: number; lng: number; key: number } | null>(null)
  const markingPanelRef = useRef<HTMLDivElement>(null)

  // 启动时载入配置与样例列表，并默认展示第一个样例。
  // 默认走预生成快照而不是实时计算，是为了让首屏不消耗任何 API 配额。
  useEffect(() => {
    Promise.all([fetchConfig(), fetchSamples()])
      .then(([cfg, list]) => {
        setConfig(cfg)
        setSamples(list)
        if (list.length > 0) return loadSample(list[0].id)
      })
      .catch((err: Error) => setError(err.message))
    fetchHistories()
      .then(setHistories)
      .catch(() => {
        // 历史库初始化失败不阻塞主流程
      })
  }, [])

  function applySample(id: string, feature: IsochroneFeature) {
    setActiveSampleId(id)
    setIsochrone(feature)
    setSimulation(null)
    // 加载的样例若正是对比项，对比自动退出，避免自己和自己比
    if (id === compareId) {
      setCompareFeature(null)
      setCompareId(null)
    }
    setMinutes(feature.properties.minutes)
    if (feature.properties.center) {
      setCenter(feature.properties.center)
      setLatText(String(feature.properties.center.lat))
      setLngText(String(feature.properties.center.lng))
    }
    closePlanning()
    setClosurePlacing(false)
    setPickEnabled(false)
    setPlacing(null)
    setNotice(null)
  }

  function loadSample(id: string) {
    return fetchSample(id)
      .then((feature) => applySample(id, feature))
      .catch((e: Error) => setError(e.message))
  }

  function loadHistory(id: number) {
    fetchHistory(id)
      .then((record) => {
        const feature = record.result
        setActiveSampleId(null)
        setIsochrone(feature)
        setSimulation(null)
        const p = feature.properties
        if (p.center) {
          setCenter(p.center)
          setLatText(String(p.center.lat))
          setLngText(String(p.center.lng))
        }
        closePlanning()
        setClosurePlacing(false)
        setPlacing(null)
        setPickEnabled(false)
        setNotice(`已载入历史记录 #${id}（不消耗 API 配额）`)
      })
      .catch((e: Error) => setError(e.message))
  }

  /**
   * 实时计算。inputSys 是这组坐标的坐标系：地图点选与地址搜索得到的已经是 BD09，
   * 只有手动输入才按用户选择的坐标系，否则 BD09 会被再转换一次、整体偏移。
   */
  const run = useCallback(
    async (
      lat: number,
      lng: number,
      inputSys: string = 'bd09',
      markingOverride?: 'auto' | 'none',
      /** 显示名：搜索的地址；同一中心点重算时沿用当前结果的名字 */
      name?: string,
    ) => {
      const useMarkings = markingOverride ?? markingMode
      closePlanning()
      setBusy(true)
      setError(null)
      setNotice(null)
      setActiveSampleId(null)
      setSimulation(null)
      setLatText(String(lat))
      setLngText(String(lng))
      setStageText(null)
      setStageId('isochrone')
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl
      try {
        // 离线模拟路径：零 API 消耗，AK 失效时的保底演示
        if (dataMode === 'demo') {
          const feature = await fetchDemo({ lat, lng, minutes, directions, mode })
          setIsochrone(feature)
          setCenter({ lat, lng })
          setNotice('已生成离线模拟结果：形状由伪随机生成，不代表真实路网。')
          return
        }
        // SSE 渐进式：等时圈先上图，设施与盲区随后补齐，遮罩文案随阶段更新
        await computeIsochroneStream(
          {
            lat,
            lng,
            minutes,
            directions,
            coverage: withCoverage,
            blindspots: withCoverage,
            mode,
            coord_sys: inputSys,
            crossing_delay: crossingDelay,
            closures: [],
            markings: { mode: useMarkings, include: adopted, exclude: [] },
            ...(name ? { name } : {}),
          },
          {
            onStage: (stage, message) => {
              setStageId(stage)
              setStageText(message)
            },
            onFeature: (feature, stage) => {
              setIsochrone(feature)
              setStageId(stage)
              if (feature.properties.center) setCenter(feature.properties.center)
            },
            onDone: (feature) => {
              setIsochrone(feature)
              // 非 BD09 输入时后端已转换坐标，地图中心必须用转换后的值，否则整体偏移
              setCenter(feature.properties.center ?? { lat, lng })
              fetchHistories()
                .then(setHistories)
                .catch(() => {})
            },
          },
          ctrl.signal,
        )
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          if (abortRef.current === ctrl) setNotice('已取消。已画出的等时圈会保留。')
          return
        }
        // 后端的说明已经写清了服务、停在哪一步、何时恢复、现在能做什么，原样展示
        if (err instanceof ApiError) {
          setError(err.message)
        } else {
          setError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        if (abortRef.current === ctrl) {
          setBusy(false)
          setStageText(null)
          setStageId(null)
        }
      }
    },
    [minutes, directions, withCoverage, mode, dataMode, crossingDelay, markingMode, adopted],
  )

  async function onMapSearch() {
    const query = address.trim()
    if (busy) return
    if (!query) {
      // 按钮不置灰（置灰像是坏了）：空着点就把光标放回输入框并说明
      searchInputRef.current?.focus()
      setNotice('先在输入框里写地址或小区名，再点「体检」；也可以直接点地图任意位置。')
      return
    }
    setError(null)
    try {
      const hit = await geocode(query)
      if (comparePicking) await runCompare(hit.lat, hit.lng)
      else await run(hit.lat, hit.lng, 'bd09', undefined, query)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  async function onSearch() {
    if (!address.trim()) return
    setBusy(true)
    setError(null)
    try {
      const hit = await geocode(address.trim())
      await run(hit.lat, hit.lng, 'bd09', undefined, address.trim())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  /** 进入对比：下一步用搜索、样例或点地图指定地点 B，可以是新地址。 */
  function toggleCompare() {
    closeTrip()
    closePlanning()
    if (compareFeature || comparePicking) {
      setCompareFeature(null)
      setCompareId(null)
      setComparePicking(false)
      setNotice(null)
      return
    }
    setComparePicking(true)
    setSamplesOpen(false)
    setNotice('对比地点可以是新地址：在左上搜索，或从样例里选，或直接点地图。')
  }

  const runCompare = useCallback(
    async (lat: number, lng: number) => {
      setBusy(true)
      setError(null)
      setComparePicking(false)
      setStageText(null)
      setStageId('isochrone')
      abortRef.current?.abort()
      const ctrl = new AbortController()
      abortRef.current = ctrl
      try {
        if (dataMode === 'demo') {
          const feature = await fetchDemo({ lat, lng, minutes, directions, mode })
          setCompareFeature(feature)
          setCompareId(null)
          setNotice('对比地点是离线模拟，不代表真实路网。')
          return
        }
        await computeIsochroneStream(
          {
            lat,
            lng,
            minutes,
            directions,
            coverage: withCoverage,
            blindspots: withCoverage,
            mode,
            // 对比地点只来自地图点选或地址搜索，均为 BD09
            coord_sys: 'bd09',
            crossing_delay: crossingDelay,
          },
          {
            onStage: (stage, message) => {
              setStageId(stage)
              setStageText(`对比地点 · ${message}`)
            },
            onFeature: (feature) => {
              setCompareFeature(feature)
              setCompareId(null)
            },
            onDone: (feature) => setCompareFeature(feature),
          },
          ctrl.signal,
        )
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return
        const message = err instanceof Error ? err.message : String(err)
        setError(`对比地点：${message}`)
      } finally {
        if (abortRef.current === ctrl) {
          setBusy(false)
          setStageId(null)
          setStageText(null)
        }
      }
    },
    [minutes, directions, withCoverage, mode, dataMode, crossingDelay],
  )

  /**
   * 模拟新建：候选方格到拟建点做一次路网测距（通常不超过 100 个点对），
   * 步行 1 公里内够得着的才消去。离线模拟数据只按直线估算上限。
   */
  function handleSimulate(p: Prescription) {
    if (p.lat == null || p.lng == null || !p.category) return
    simulateAt(p.category, p.lat, p.lng)
  }

  function simulateAt(category: string, lat: number, lng: number) {
    if (!isochrone) return
    closeTrip()
    setComposer(null); setCompareFeature(null); setCompareId(null); setComparePicking(false)
    setPlanningMode('facility'); setClosurePlacing(false)
    setPlacing(category)
    setNotice(null)
    const request = ++planningRequest.current
    planningController.current?.abort()
    const controller = new AbortController()
    planningController.current = controller
    setSimulation(null); setError(null)
    setPlanningBusy(true)
    simulate({
      category,
      lat,
      lng,
      feature: isochrone,
      verify: !isochrone.properties.simulated,
    }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted || request !== planningRequest.current) return
        setSimulation(result)
      })
      .catch((err: Error) => { if (!controller.signal.aborted && request === planningRequest.current) setError(err.message) })
      .finally(() => { if (request === planningRequest.current) setPlanningBusy(false) })
  }

  useEffect(() => {
    planningRequest.current += 1
    planningController.current?.abort()
  }, [isochrone])

  function closePlanning() {
    planningRequest.current += 1
    planningController.current?.abort()
    setPlanningBusy(false); setPlanningMode(null); setPlacing(null)
    setNotice(null)
    setClosurePlacing(false); setSimulation(null); setClosurePreview(null); setClosures([])
  }

  useEffect(() => {
    if (!planningMode) return
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        planningRequest.current += 1; planningController.current?.abort()
        setPlanningBusy(false); setPlanningMode(null); setPlacing(null)
        setNotice(null)
        setClosurePlacing(false); setSimulation(null); setClosurePreview(null); setClosures([])
      }
    }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [planningMode])

  function openPlanning(mode: 'facility' | 'closure') {
    closeTrip(); setComposer(null); setComparePicking(false); setPickEnabled(false)
    setCompareFeature(null); setCompareId(null)
    planningRequest.current += 1; planningController.current?.abort(); setPlanningBusy(false)
    setPlanningMode(mode); setSimulation(null)
    setNotice(null)
    setPlacing(mode === 'facility' ? keyCategories[0] ?? '生鲜采买' : null)
    setClosurePlacing(mode === 'closure')
  }

  async function previewClosure() {
    if (!isochrone) return
    const request = ++planningRequest.current
    planningController.current?.abort()
    const controller = new AbortController()
    planningController.current = controller
    setPlanningBusy(true); setError(null)
    try {
      const preview = await simulateClosures(isochrone, closures, controller.signal)
      if (request === planningRequest.current) { setClosurePreview(preview); setClosurePlacing(false) }
    } catch (error) {
      if (!controller.signal.aborted && request === planningRequest.current) setError(error instanceof Error ? error.message : String(error))
    } finally { if (request === planningRequest.current) setPlanningBusy(false) }
  }

  function addClosure(lat: number, lng: number, radius = closureRadius, label?: string) {
    const limit = config?.closure_limits?.max_count ?? 20
    if (closures.length >= limit) {
      setNotice(`最多标注 ${limit} 处围挡。`)
      return false
    }
    const lo = config?.closure_limits?.min_radius_m ?? 10
    const hi = config?.closure_limits?.max_radius_m ?? 300
    const r = Math.round(Math.min(hi, Math.max(lo, radius)))
    planningRequest.current += 1
    planningController.current?.abort()
    setPlanningBusy(false)
    setClosures([...closures, { lat, lng, radius_m: r, ...(label ? { label } : {}) }])
    setClosurePreview(null)
    return true
  }

  /** 复测巡检：跳过缓存重取这份结果里各方向的步行路线，与保存的路线比。 */
  async function runRecheck() {
    if (!isochrone || closureBusy) return
    setClosureBusy('recheck')
    setError(null)
    const owner = isochrone
    try {
      const result = await recheck(owner)
      setRecheckEntry({ owner, value: result })
      setNotice(
        `复测了 ${result.checked} 个方向（${result.route_requests} 次步行路线规划）：` +
          (result.suspects.length > 0
            ? `${result.suspects.length} 处疑似新增阻断，请逐个确认。`
            : `没有发现明显变长的路线${result.shorter ? `，${result.shorter} 个方向反而变短` : ''}。`),
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setClosureBusy(null)
    }
  }

  /** 工地 POI 候选：2 次地点检索，按是否压在步行路线上排序。 */
  async function runConstruction() {
    if (closureBusy) return
    const owner = isochrone
    const c = owner?.properties.center ?? center
    setClosureBusy('poi')
    setError(null)
    try {
      const result = await constructionCandidates({
        lat: c.lat,
        lng: c.lng,
        radius_m: owner?.properties.blindspots?.extent_m ?? 1500,
        feature: owner,
      })
      setConstructionEntry({ owner, value: result })
      setNotice(
        `工地检索：${result.raw_count} 条结果，筛掉 ${result.dropped} 条店铺与公司，` +
          `留下 ${result.candidates.length} 处候选（${result.poi_queries} 次地点检索）。`,
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setClosureBusy(null)
    }
  }

  function confirmCandidate(key: string, lat: number, lng: number, radius: number, label: string) {
    if (addClosure(lat, lng, radius, label)) dismiss(key)
  }

  /** 核验选址：对某一品类的补设处方取几个备选点做路网核验，最好的一处直接显示在地图上。 */
  function handleVerifySite(category: string) {
    if (!isochrone || sitePlanBusy) return
    setSitePlanBusy(category)
    setError(null)
    const owner = isochrone
    sitePlan({ feature: owner, category })
      .then((result) => {
        setSitePlanEntry({ owner, value: result })
        // 等结果期间用户换了地点：不要把旧结果的模拟画到新结果上
        if (currentRef.current !== owner) return
        setSimulation(result.best.simulation)
        const best = result.best
        setNotice(
          `核验选址「${category}」：` +
            (result.verified
              ? `${result.candidates.filter((c) => c.status === 'verified').length} 个备选点路网实测，` +
                `最好的一处消去 ${best.verified ?? best.simulation.covered_count} 格（${result.pairs_used} 个点对）。`
              : '未做路网核验，只按直线估算。'),
        )
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setSitePlanBusy(null))
  }

  /**
   * AI 二次核对：用百度地图 Agent Plan 再找一遍每片灰色区域附近缺的关键设施。
   * 只给线索：疑似漏收录的设施可以按补录模拟一次，或到现场确认后共享为「补录设施」标注。
   */
  function runCrosscheck() {
    if (!isochrone || crosscheckBusy) return
    setCrosscheckBusy(true)
    setError(null)
    const owner = isochrone
    crosscheckFeature(owner)
      .then((result) => {
        setCrosscheckEntry({ owner, value: result })
        if (currentRef.current !== owner) return
        const asked = result.rows.length
        const fresh = result.agent_plan.requests
        const cost = fresh > 0 ? `新问 ${fresh} 个，其余读缓存` : '全部读缓存'
        setNotice(
          result.aborted
            ? `AI 二次核对没有完成：${result.aborted}`
            : `AI 二次核对：问了 ${asked} 个问题（${cost}），` +
                (result.suspects.length > 0
                  ? `发现 ${result.suspects.length} 处疑似漏收录的设施，请逐个确认。`
                  : '缺口附近的同类设施都已收录，盲区不是漏检造成的。'),
        )
      })
      .catch((err: Error) => setError(err.message))
      .finally(() => setCrosscheckBusy(false))
  }

  /** 疑似漏收录的设施 → 新建「补录设施」标注：位置、类别、名称都带好，照片仍要到现场拍。 */
  function shareSuspect(s: CrosscheckSuspect) {
    openComposer({
      type: 'facility_extra',
      point: { lat: s.lat, lng: s.lng },
      category: s.category,
      name: s.name,
      source: 'agent_plan',
      context: '来自 AI 二次核对（百度地图 Agent Plan）',
    })
  }

  // 标注的围挡与当前结果用到的不一致时，提醒要重新计算才生效
  const appliedClosures = closurePreview?.properties.closures ?? []
  const closuresDirty =
    JSON.stringify(appliedClosures.map((c) => [c.lat, c.lng, c.radius_m])) !==
    JSON.stringify(closures.map((c) => [c.lat, c.lng, c.radius_m]))

  // 品类词表由后端下发，图层筛选与判定口径因此不会各写一份而对不上
  const keyCategories = (config?.categories ?? [])
    .filter((c) => c.key_facility)
    .map((c) => c.name)
  const props = isochrone?.properties
  const report = props?.report
  const coverage = props?.coverage
  // 复测要有路线基线：新版步行结果每个方向都存了路线，旧快照、离线模拟、非步行没有
  const baselineRays = (props?.rays ?? []).filter((r) => (r.route_path?.length ?? 0) >= 2).length
  const canRecheck = baselineRays > 0 && !props?.simulated && (props?.mode ?? 'walk') === 'walk'
  // 传给地图的数组必须记忆化：每次渲染都新建数组会让地图覆盖物整层重建
  const suspects = useMemo(
    () => (recheckResult?.suspects ?? []).filter((s) => !dismissed.includes(s.id)),
    [recheckResult, dismissed],
  )
  const sites = useMemo(
    () =>
      (constructionResult?.candidates ?? []).filter(
        (c) => !dismissed.includes(`poi-${c.lat}-${c.lng}`),
      ),
    [constructionResult, dismissed],
  )
  const recheckRays = useMemo(
    () => (recheckResult?.rays ?? []).filter((r) => r.status !== 'same'),
    [recheckResult],
  )

  // ---------- 共享标注：拉取、选择、新建 ----------
  const markingConfig = config?.markings
  const markingCenter = props?.center ?? center
  const mLat = markingCenter.lat
  const mLng = markingCenter.lng
  // 与后端分析时的查询半径一致：网格范围 + 1 公里判定阈值
  const mRadius = (props?.blindspots?.extent_m ?? 1500) + (config?.walk_limit_m ?? 1000)
  const nearbyKey = `${mLat.toFixed(6)},${mLng.toFixed(6)},${Math.round(mRadius)},${markingRevision}`
  const markingsReady = Boolean(markingConfig)
  useEffect(() => {
    if (!markingsReady) return
    let cancelled = false
    const key = `${mLat.toFixed(6)},${mLng.toFixed(6)},${Math.round(mRadius)},${markingRevision}`
    fetchMarkings(mLat, mLng, mRadius)
      .then((items) => {
        if (cancelled) return
        setNearbyEntry({ key, items })
        setMarkingsError(null)
      })
      .catch((err: Error) => {
        if (!cancelled) setMarkingsError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [markingsReady, mLat, mLng, mRadius, markingRevision])
  const nearbyMarkings = nearbyEntry?.items ?? NO_MARKINGS
  const markingsLoading = markingsReady && nearbyEntry?.key !== nearbyKey

  const refreshMarkings = useCallback(() => setMarkingRevision((r) => r + 1), [])
  const showToast = useCallback(
    (t: Omit<ToastSpec, 'id'>) => setToast({ ...t, id: Date.now() + Math.random() }),
    [],
  )

  function revealMarking(id: number) {
    setSelectedMarkingId(id)
    // 侧栏可能滚在很下面：把标注卡片滚进视野，点了地图上的徽标马上能看到详情
    window.requestAnimationFrame(() =>
      markingPanelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    )
  }

  function locateMarking(m: Marking) {
    setMapFocus({ lat: m.lat, lng: m.lng, key: Date.now() })
    setSelectedMarkingId(m.id)
  }

  /** 打开新建标注（进入标注模式）。从地图上点进来时类型、位置、类别与原因已带好。 */
  function openComposer(preset?: ComposerPreset, preserveTrip = false) {
    if (!markingConfig) return
    closePlanning()
    if (preserveTrip) closeGuide()
    else closeTrip()
    setClosurePlacing(false)
    setPlacing(null)
    setComparePicking(false)
    setSelectedMarkingId(null)
    setSamplesOpen(false)
    const [lo, hi] = markingConfig.limits.closure_radius_m
    const radius = Math.min(hi, Math.max(lo, Math.round(preset?.radius ?? 50)))
    const type: MarkingType = preset?.type ?? 'closure'
    draftHistory.reset({
      ...emptyDraft(type, radius),
      point: preset?.point ?? null,
      place: preset?.place ?? null,
      vertices: preset?.vertices ?? [],
    })
    const step: ComposerStep = !preset
      ? 'type'
      : type === 'gray_area' && !preset.reason
        ? 'why'
        : (type === 'facility_extra' && preset.point && preset.name && preset.sellsVegetables) || (type === 'facility_missing' && preset.place && preset.reason)
          ? 'details'
          : 'place'
    // 侧栏要收起：记下滚动位置，退出标注模式时回到原处
    if (!composer) sidebarScrollRef.current = sidebarRef.current?.scrollTop ?? 0
    setRailOpen(false)
    setComposer({ key: Date.now(), source: preset?.source ?? 'user', step, preset: preset ?? null })
    const anchor = preset?.point ?? preset?.vertices?.[0]
    if (anchor) setReveal({ ...anchor, key: Date.now(), rightInset: 400 })
  }

  function handleIntent(intent: MapIntent) {
    switch (intent.kind) {
      case 'trip-to':
        openTrip(intent.place.category, undefined, intent.place)
        return
      case 'trip-from-cell':
        openTrip(intent.category, { lat: intent.cell.lat, lng: intent.cell.lng, kind: 'cell' })
        return
      case 'compose':
        openComposer(intent.preset)
        return
      case 'temp-closure':
        openPlanning('closure')
        if (addClosure(intent.lat, intent.lng, intent.radius, intent.label)) {
          dismiss(intent.key)
          setNotice('已加入假设围挡，在规划模拟中评估，不影响正式出行。')
        }
        return
      case 'dismiss':
        dismiss(intent.key)
        return
      case 'select-marking':
        revealMarking(intent.id)
    }
  }

  const railMode = (composer !== null || Boolean(scoped(tripEntry, isochrone))) && !railOpen
  useLayoutEffect(() => {
    if (railMode || !sidebarRef.current) return
    sidebarRef.current.scrollTop = sidebarScrollRef.current
  }, [railMode])

  function handleMarkingCreated(marking: Marking) {
    setComposer(null)
    refreshMarkings()
    if (!scoped(tripEntry, isochrone)) revealMarking(marking.id)
    showToast({
      message: scoped(tripEntry, isochrone)
        ? '已提交补录，已返回原行程；下次查询时应用这条标注。'
        : `已提交共享标注（附 ${marking.photo_count} 张现场照片）：对你立即生效，重新计算即可看到影响`,
      tone: 'ok',
      undoLabel: '撤回',
      undo: async () => {
        await retractMarking(marking.id)
        refreshMarkings()
      },
    })
  }

  function toggleAdopt(id: number) {
    setAdopted((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]))
  }

  function rerunWithMarkings(nextMode: 'auto' | 'none') {
    setMarkingMode(nextMode)
    void run(center.lat, center.lng, 'bd09', nextMode, isochrone?.properties.name)
  }

  // 这份结果之后新出现、按默认规则本该计入却没计入的附近标注（已核实的，或自己的）
  const appliedIds = new Set((props?.markings?.applied ?? []).map((m) => m.id))
  const unapplied =
    isochrone && !props?.simulated && markingMode === 'auto'
      ? nearbyMarkings.filter((m) => (m.status === 'verified' || m.mine) && !appliedIds.has(m.id))
      : NO_MARKINGS
  // 「纯算法 / 含标注」：能即时切换时地图用还原出来的纯算法结果，侧栏报告始终是页面上的结果
  const switcher = viewSwitch(isochrone)
  const switchKind = switcher.kind
  const resultView: ResultView =
    switchKind === 'instant'
      ? (scoped(viewEntry, isochrone) ?? 'markings')
      : switcher.kind === 'rerun' && switcher.to === 'auto'
        ? 'algorithm'
        : 'markings'
  const mapFeature = useMemo(
    () =>
      isochrone && switchKind === 'instant' && resultView === 'algorithm'
        ? algorithmView(isochrone)
        : isochrone,
    [isochrone, switchKind, resultView],
  )
  const trip = scoped(tripEntry, isochrone)
  const tripMap = trip ? scoped(tripMapEntry, isochrone) : null
  const guide = useMemo(() => {
    const value = trip && !composer ? scoped(guideEntry, isochrone) : null
    if (!value || !isochrone) return null
    // 开发热更新时，已经打开的旧引导状态也要能恢复，不丢掉原路线。
    return value.remaining && value.routingFeature && value.mode ? value : {
      ...value, remaining: [value.item], routingFeature: isochrone,
      mode: 'preview' as const, focus: 'origin' as const, focusKey: value.focusKey + 1,
    }
  }, [trip, composer, guideEntry, isochrone])
  const updateGuide = useCallback((value: TripGuideData) => setGuideEntry({ owner: isochrone, value }), [isochrone])
  function openGuide(item: TripItem, scope?: 'journey' | 'segment') {
    if (guideUnavailableReason(item, tripMap?.preview)) return
    if (!isochrone) return
    const index = tripMap?.itinerary ? tripMap.items.findIndex(leg => leg.entry_id === item.entry_id) : -1
    const selection = index >= 0 ? guideItinerary(tripMap!.items, item, scope ?? 'journey') : { item, remaining: [item], context: undefined }
    if (guideUnavailableReason(selection.item, tripMap?.preview)) return
    updateGuide({ ...selection, routingFeature: isochrone, mode: 'preview', stepIndex: 0, focus: 'origin', focusKey: 0, location: null })
  }
  const updateTripMap = useCallback((value: TripMapData) => {
    setTripMapEntry({ owner: isochrone, value })
    if (value.preview) return // 验收假数据不得进入真实共享标注。
    setTripPlacesEntry(previous => {
      const known = scoped(previous, isochrone) ?? []
      const additions = value.items.filter(item => !known.some(p => p.id === item.place_id))
      if (!additions.length) return previous
      return { owner: isochrone, value: [...known, ...additions.map(item => item.place ?? {
        id: item.place_id, name: item.name, category: item.category,
        lat: item.lat, lng: item.lng, in_circle: item.in_circle,
        fresh_status: item.fresh_status, fresh_evidence: item.fresh_evidence,
      })] }
    })
  }, [isochrone])
  const markingPlaces = useMemo(() => {
    const places = [...(isochrone?.properties.coverage?.places ?? [])]
    for (const place of scoped(tripPlacesEntry, isochrone) ?? []) {
      if (!places.some(p => p.id === place.id || (p.category === place.category && p.name === place.name && metersBetween(p, place) <= 50))) places.push(place)
    }
    return places
  }, [isochrone, tripPlacesEntry])
  function openTrip(category?: string, origin?: TripOrigin, target?: Place) {
    if (!isochrone?.properties.center) return
    closePlanning()
    setComposer(null); setPlacing(null); setSimulation(null); setComparePicking(false)
    setCompareFeature(null); setCompareId(null); setClosurePlacing(false); setPickEnabled(false)
    setTripPicking(false); setTripSelection(null); setTripMapEntry(null)
    closeGuide()
    sidebarScrollRef.current = sidebarRef.current?.scrollTop ?? sidebarScrollRef.current
    setRailOpen(false)
    const start = origin ?? (target && trip ? trip.origin : { ...isochrone.properties.center, kind: 'center' as const })
    setTripEntry({ owner: isochrone, value: { key: Date.now(), origin: start, category, targetId: target?.id } })
    setReveal({ ...start, key: Date.now(), rightInset: 400 })
  }
  function changeTripOrigin(origin: TripOrigin) {
    if (!trip || !mapFeature?.properties.center) return
    if (metersBetween(mapFeature.properties.center, origin) > (mapFeature.properties.coverage?.radius_m ?? 2500)) {
      setNotice('起点超出设施检索范围，请先在那里体检一次。'); return
    }
    setTripPicking(false)
    setTripEntry({ owner: isochrone, value: { ...trip, origin } })
    setReveal({ ...origin, key: Date.now(), rightInset: 400 })
  }
  // 围挡改了圈时，另一种看法的外圈画成点线对比
  const baselineRing = props?.markings?.baseline?.ring ?? null
  const altRing =
    baselineRing && isochrone
      ? resultView === 'algorithm' && switchKind === 'instant'
        ? (isochrone.geometry.coordinates[0] ?? null)
        : baselineRing
      : null
  const composerPicking = composer?.step === 'place'

  return (
    <div className={guide ? 'app guiding' : trip && !composer ? 'app travelling' : 'app'}>
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">途</span>
          <div className="brand-text">
            <h1>路遥识途</h1>
            <p>基于百度地图真实路网的 15 分钟生活圈体检与规划助手</p>
          </div>
        </div>
        <div className="topbar-spacer" />
        {!activeSampleId && (
          <span className={props?.simulated ? 'source-chip demo' : 'source-chip live'}>
            <i />
            {props?.simulated ? '离线模拟 · 非真实路网数据' : '实时计算结果'}
          </span>
        )}
        {markingConfig?.admin_enabled && (
          <button
            type="button"
            className="admin-entry"
            aria-pressed={adminOpen}
            onClick={() => setAdminOpen((v) => !v)}
          >
            <Icon name="shield" size={14} />
            {adminOpen ? '关闭审核' : '标注审核'}
          </button>
        )}
      </header>

      <div className={railMode ? 'layout rail' : 'layout'}>
        {railMode && (
          <aside className="sidebar-rail" aria-label="侧栏已收起">
            <button
              type="button"
              title="展开体检报告（地图会变窄）"
              aria-label="展开侧栏"
              onClick={() => setRailOpen(true)}
            >
              <Icon name="chevron" size={16} />
            </button>
            <span>体检报告</span>
          </aside>
        )}
        <aside className="sidebar" ref={sidebarRef}>
          {(trip || composer) && railOpen && <button type="button" className="trip-sidebar-collapse" onClick={() => { sidebarScrollRef.current = sidebarRef.current?.scrollTop ?? 0; setRailOpen(false) }}>收起体检报告 ‹</button>}
          {isochrone && <Verdict feature={isochrone} />}

          <details className="card">
            <summary>历史与导出</summary>
          {histories.length > 0 && (
            <section>
              <h2 className="card-title">历史记录</h2>
              <div className="history-list">
                {histories.map((h) => (
                  <button
                    key={h.id}
                    type="button"
                    className="history-item"
                    onClick={() => loadHistory(h.id)}
                  >
                    <span className="history-score">
                      {h.score != null ? `${Math.round(h.score)} 分` : '—'}
                    </span>
                    <span className="history-meta">
                      {h.minutes} 分钟 · {h.area_km2 != null ? `${h.area_km2.toFixed(2)} km²` : ''} ·{' '}
                      {new Date(h.created_at).toLocaleString('zh-CN', { hour12: false })}
                    </span>
                  </button>
                ))}
              </div>
            </section>
          )}

          </details>

          <details className="card live-panel">
            <summary>实时计算（消耗配额）</summary>
            <div className="field">
              <span className="field-label">数据模式</span>
              <div className="mode-pills">
                <button
                  type="button"
                  className={dataMode === 'real' ? 'pill active' : 'pill'}
                  onClick={() => setDataMode('real')}
                >
                  真实百度 API
                </button>
                <button
                  type="button"
                  className={dataMode === 'demo' ? 'pill active' : 'pill'}
                  onClick={() => setDataMode('demo')}
                >
                  离线模拟
                </button>
              </div>
              <p className="hint">
                离线模拟用确定性伪随机生成结果，零 AK、零配额，仅用于演示 UI 与链路；
                页面会明确标注，不冒充真实路网结论。
              </p>
            </div>

            <div className="field">
              <span className="field-label">坐标系</span>
              <select
                className="select"
                value={coordSys}
                onChange={(e) => setCoordSys(e.target.value)}
              >
                <option value="bd09">BD09LL（百度）</option>
                <option value="gcj02">GCJ02（高德 / 腾讯）</option>
                <option value="wgs84">WGS84（GPS）</option>
              </select>
              <p className="hint">非 BD09 坐标会先经百度坐标转换，转换无日配额限制。</p>
            </div>

            <div className="field">
              <span className="field-label">中心点坐标（纬度 / 经度）</span>
              <div className="row">
                <input
                  type="number"
                  step="any"
                  aria-label="纬度"
                  value={latText}
                  onChange={(e) => setLatText(e.target.value)}
                />
                <input
                  type="number"
                  step="any"
                  aria-label="经度"
                  value={lngText}
                  onChange={(e) => setLngText(e.target.value)}
                />
                <button
                  disabled={busy}
                  onClick={() => {
                    const la = Number(latText)
                    const ln = Number(lngText)
                    if (Number.isFinite(la) && Number.isFinite(ln)) run(la, ln, coordSys)
                  }}
                >
                  计算
                </button>
              </div>
              <p className="hint">
                地图点选与地址搜索会自动回填（BD09），不再转换；只有点这里的「计算」才按上方坐标系转换。
              </p>
            </div>

            <div className="field">
              <span className="field-label">出行方式</span>
              <div className="mode-pills">
                {(config?.modes?.length
                  ? config.modes
                  : [
                      { id: 'walk', label: '步行' },
                      { id: 'ride', label: '骑行' },
                      { id: 'drive', label: '驾车（畅通路况）' },
                      { id: 'drive_traffic', label: '驾车（实时路况）' },
                    ]
                ).map((m) => (
                  <button
                    key={m.id}
                    type="button"
                    className={mode === m.id ? 'pill active' : 'pill'}
                    onClick={() => setMode(m.id)}
                  >
                    {m.label}
                  </button>
                ))}
              </div>
              <p className="hint">
                步行是命题口径。骑行/驾车走另一张路网；「实时路况」与「畅通路况」对照即拥堵损失。
                红绿灯等待已计入耗时，接口不能按灯拆开。网格盲区只在步行时计算。
              </p>
            </div>

            <div className="field">
              <label htmlFor="address">地址搜索</label>
              <div className="row">
                <input
                  id="address"
                  value={address}
                  placeholder="如：上海市普陀区桃浦镇"
                  onChange={(e) => setAddress(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && onSearch()}
                />
                <button onClick={onSearch} disabled={busy}>
                  定位
                </button>
              </div>
            </div>

            <div className="field">
              <label htmlFor="minutes">时间阈值：{minutes} 分钟</label>
              <input
                id="minutes"
                type="range"
                min={5}
                max={30}
                step={5}
                value={minutes}
                onChange={(e) => setMinutes(Number(e.target.value))}
              />
            </div>

            <div className="field">
              <label htmlFor="directions">方向数：{directions} 条射线</label>
              <input
                id="directions"
                type="range"
                min={12}
                max={72}
                step={12}
                value={directions}
                onChange={(e) => setDirections(Number(e.target.value))}
              />
              <p className="hint">
                方向越多轮廓越精细。当前方式每请求最多 {config?.modes.find(m => m.id === mode)?.matrix_batch_pairs ?? 50} 个点对，
                当前设置冷缓存约 {Math.ceil((directions * 7) / (config?.modes.find(m => m.id === mode)?.matrix_batch_pairs ?? 50))} 次。
              </p>
            </div>

            <label className="check">
              <input
                type="checkbox"
                checked={withCoverage}
                onChange={(e) => setWithCoverage(e.target.checked)}
              />
              同时做设施覆盖与盲区判定
            </label>
            <p className="hint">关闭后只算等时圈，不消耗地点检索配额，出分也只含路网三项。</p>

            <label className="check">
              <input
                type="checkbox"
                checked={crossingDelay}
                onChange={(e) => setCrossingDelay(e.target.checked)}
              />
              补回过街与路口等待（步行）
            </label>
            <p className="hint">
              批量算路的耗时只是距离 ÷ 步速，不含红绿灯与天桥。开启后每个方向另取一条步行路线，
              约 {directions} 次路线规划；标注了施工围挡时也要靠它判断路线是否被挡。
            </p>

            <label className="check">
              <input
                type="checkbox"
                checked={pickEnabled}
                onChange={(e) => setPickEnabled(e.target.checked)}
              />
              允许点击地图重新计算
            </label>
            <p className="hint">默认关闭。误点一次就会烧掉一批算路点对。</p>

            <button
              className="primary"
              onClick={() => run(center.lat, center.lng, 'bd09', undefined, props?.name)}
              disabled={busy}
            >
              {busy ? '计算中…' : '重新计算当前中心点'}
            </button>
          </details>

          {(config?.warnings ?? []).map((w) => (
            <div key={w.code} className="banner error" role="alert">
              {w.message}
            </div>
          ))}
          {error && (
            <div className="banner error" role="alert">
              {error}
            </div>
          )}
          {notice && !error && <div className="banner notice">{notice}</div>}

          {compareFeature && isochrone?.properties.report && (
            <CompareCard
              a={isochrone}
              b={compareFeature}
              onClose={() => {
                setCompareFeature(null)
                setCompareId(null)
              }}
            />
          )}
          {isochrone && markingConfig && unapplied.length > 0 && !props?.markings && (
            <div className="banner notice mk-banner" role="status">
              <span>
                附近有 {unapplied.length} 条
                {unapplied.some((m) => m.mine) ? '已核实或你自己的' : '已核实的'}
                共享标注，这份结果还没有计入。
              </span>
              <button
                type="button"
                className="mk-btn small"
                disabled={busy}
                onClick={() => rerunWithMarkings('auto')}
              >
                按标注重新计算
              </button>
            </div>
          )}
          {props?.markings &&
            markingConfig &&
            (props.markings.nearby_count > 0 || props.markings.mode === 'none') && (
              <MarkingEffectCard
                result={props.markings}
                adopted={adopted}
                onToggleAdopt={toggleAdopt}
                pendingNew={unapplied.length}
                mode={markingMode}
                onRerun={rerunWithMarkings}
                onSelect={revealMarking}
                busy={busy}
                mapView={
                  switchKind === 'instant'
                    ? {
                        view: resultView,
                        onChange: (view) => setViewEntry({ owner: isochrone, value: view }),
                      }
                    : undefined
                }
              />
            )}
          {isochrone && report && <NarrativeCard feature={isochrone} />}
          {report && (
            <ReportCard
              report={report}
              coverage={coverage}
              meta={props}
              simulation={planningMode === 'facility' ? simulation : null}
              onSimulate={handleSimulate}
              onClearSimulation={() => setSimulation(null)}
              sitePlan={sitePlanResult}
              sitePlanBusy={sitePlanBusy}
              onVerifySite={handleVerifySite}
              crosscheck={crosscheckResult}
              crosscheckBusy={crosscheckBusy}
              onCrosscheck={
                config?.agent_plan?.configured && !props?.simulated ? runCrosscheck : undefined
              }
              onSimulateSuspect={(s) => simulateAt(s.category, s.lat, s.lng)}
              onShareSuspect={markingConfig ? shareSuspect : undefined}
              onTrip={(category) => openTrip(category)}
            />
          )}

          {markingConfig && (
            <div ref={markingPanelRef} className="mk-panel-anchor">
              <MarkingPanel
                config={markingConfig}
                nearby={nearbyMarkings}
                loading={markingsLoading}
                error={markingsError}
                radiusM={mRadius}
                selectedId={selectedMarkingId}
                onSelect={setSelectedMarkingId}
                onCreate={() => openComposer()}
                onChanged={refreshMarkings}
                onToast={showToast}
                onLocate={locateMarking}
                revision={markingRevision}
              />
            </div>
          )}

          {isochrone && (
            <ExportCard feature={isochrone} sampleId={activeSampleId} />
          )}

          {props && report && <SignatureCard meta={props} report={report} />}
        </aside>

        <main className="stage">
          {!composer && (
          <div className={comparePicking ? 'map-search compare' : 'map-search'}>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                void onMapSearch()
              }}
            >
              {comparePicking && <span className="search-tag">对比地点</span>}
              <input
                ref={searchInputRef}
                value={address}
                aria-label={comparePicking ? '对比地点的地址或小区名' : '地址或小区名'}
                placeholder={
                  comparePicking
                    ? '输入要对比的小区或地址'
                    : '输入地址或小区名，或从样例打开'
                }
                onChange={(e) => setAddress(e.target.value)}
              />
              <button
                type="submit"
                disabled={busy}
                title="按地址实时计算：新地点会消耗百度配额，算过的地点走缓存"
              >
                {busy ? '计算中…' : comparePicking ? '对比' : '体检'}
              </button>
              {comparePicking ? (
                <button type="button" onClick={toggleCompare}>
                  取消
                </button>
              ) : (
                <button
                  type="button"
                  aria-expanded={samplesOpen}
                  onClick={() => setSamplesOpen((v) => !v)}
                >
                  样例
                </button>
              )}
            </form>
            {samplesOpen && (
              <ul className="samples-menu">
                {samples.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setSamplesOpen(false)
                        if (comparePicking) {
                          fetchSample(s.id)
                            .then((feature) => {
                              setCompareFeature(feature)
                              setCompareId(s.id)
                              setComparePicking(false)
                              setNotice(null)
                            })
                            .catch((err: Error) => setError(err.message))
                        } else {
                          void loadSample(s.id)
                        }
                      }}
                    >
                      {shortName(s.name)}
                      {s.total != null ? ` · ${Math.round(s.total)} 分` : ''}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          )}
          {!composer && (
          <div className="map-tools floating">
            <button
              type="button"
              className={compareFeature || comparePicking ? 'tool on' : 'tool'}
              onClick={toggleCompare}
            >
              {compareFeature || comparePicking ? '退出对比' : '对比'}
            </button>
            <button type="button" className={trip ? 'tool on' : 'tool'} disabled={!mapFeature?.properties.coverage || busy}
              aria-pressed={Boolean(trip)} onClick={() => trip ? closeTrip() : openTrip()}><Icon name="route" /> 出行</button>
            <button type="button" className={planningMode ? 'tool on' : 'tool'}
              onClick={() => planningMode ? closePlanning() : openPlanning('facility')}>
              规划模拟
            </button>
            {markingConfig && (
              <button
                type="button"
                className="tool share"
                title="保存下来，附近的人分析时也能用上。也可以直接点地图上的方格、设施"
                onClick={() => openComposer()}
              >
                共享标注
              </button>
            )}
            <button type="button" className="tool tool-secondary" onClick={() => window.print()}>
              导出 PDF
            </button>
            <details className="map-more"><summary className="tool">更多 ▾</summary><div className="floating">
              <button type="button" className="tool" onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); window.print() }}>导出 PDF</button>
            </div></details>
          </div>
          )}
          {planningMode === 'facility' && placing && (
            <PlanningDrawer mode="facility" busy={planningBusy} onMode={openPlanning} onClose={closePlanning}
              footer={<button type="button" className="trip-secondary-action" disabled={!simulation && !planningBusy}
                onClick={() => { planningRequest.current += 1; planningController.current?.abort(); setSimulation(null); setPlanningBusy(false) }}>
                {planningBusy ? '取消评估' : '撤销拟建点'}
              </button>}>
              <FacilitySimulation category={placing} result={simulation} busy={planningBusy} onCategory={name => {
                if (name === placing) return
                planningRequest.current += 1; planningController.current?.abort()
                setPlanningBusy(false); setSimulation(null); setPlacing(name); setError(null)
              }} />
            </PlanningDrawer>
          )}
          {composer && markingConfig && (
            <MarkingComposer
              key={composer.key}
              config={markingConfig}
              history={draftHistory}
              source={composer.source}
              step={composer.step}
              onStep={(step) => setComposer((c) => (c ? { ...c, step } : c))}
              preset={composer.preset}
              places={markingPlaces}
              rays={props?.rays ?? []}
              center={markingCenter}
              onCreated={handleMarkingCreated}
              onFocusExisting={(m) => {
                setComposer(null)
                locateMarking(m)
                revealMarking(m.id)
              }}
              onClose={() => setComposer(null)}
            />
          )}
          {!composer && !trip && planningMode === 'closure' && (
            <PlanningDrawer mode="closure" busy={planningBusy} onMode={openPlanning} onClose={closePlanning}
              footer={<>
                <button type="button" className="trip-primary-action" disabled={planningBusy || !closuresDirty || !isochrone || props?.simulated} onClick={() => void previewClosure()}>{planningBusy ? '评估中…' : '评估封闭效果'}</button>
                <button type="button" className="trip-secondary-action" disabled={closures.length === 0} onClick={() => { planningRequest.current += 1; planningController.current?.abort(); setPlanningBusy(false); setClosures([]); setClosurePreview(null) }}>清空假设围挡</button>
              </>}>
              <section className="planning-section"><h3>假设围挡<span>{closures.length} 处</span></h3>
                <p className="trip-note">在地图上放置围挡，比较生活圈和评分变化。预览不会改变正式报告和出行路线。</p>
                <button type="button" className="trip-secondary-action" onClick={() => setClosurePlacing(v => !v)}>{closurePlacing ? '结束点选' : '在地图上添加围挡'}</button>
              </section>
              <details className="closure-auto"><summary>查找疑似围挡线索</summary>
                <div className="closure-auto-actions">
                  <button
                    type="button"
                    disabled={!canRecheck || closureBusy !== null || busy}
                    title={
                      canRecheck
                        ? '跳过缓存重新规划各方向步行路线，与这份结果保存的路线比较'
                        : '这份结果没有保存各方向的步行路线（旧版快照、离线模拟或非步行），先实时计算一次'
                    }
                    onClick={() => void runRecheck()}
                  >
                    {closureBusy === 'recheck'
                      ? '复测中…'
                      : `复测巡检（${baselineRays || 36} 次路线规划）`}
                  </button>
                  <button
                    type="button"
                    disabled={closureBusy !== null || busy || props?.simulated}
                    onClick={() => void runConstruction()}
                  >
                    {closureBusy === 'poi' ? '检索中…' : '查工地 POI（2 次地点检索）'}
                  </button>
                </div>
                {!canRecheck && (
                  <p className="hint">
                    复测要和上次的路线比：这份结果没有路线基线，先开启过街等待校正实时计算一次。
                  </p>
                )}
                {recheckResult && (
                  <p className="hint">
                    复测 {recheckResult.checked} 个方向：{recheckResult.same} 个不变、
                    {recheckResult.longer} 个变长、{recheckResult.shorter} 个变短、
                    {recheckResult.rerouted} 个改道但长度相近
                    {recheckResult.failed ? `、${recheckResult.failed} 个没取到` : ''}。
                    {recheckResult.baseline.age_days != null
                      ? `基线是 ${recheckResult.baseline.age_days} 天前的路线。`
                      : ''}
                  </p>
                )}
                {suspects.length > 0 && (
                  <ul className="candidate-list" aria-label="复测发现的疑似阻断">
                    {suspects.map((s) => (
                      <li key={s.id}>
                        <div>
                          <b>{s.label}</b>
                          <span>
                            +{s.max_delta_m} 米 · 半径 {s.radius_m} 米{s.precise ? '' : ' · 位置较粗'}
                          </span>
                          <p>{s.reason}</p>
                        </div>
                        <footer>
                          <button
                            type="button"
                            className="primary"
                            onClick={() =>
                              confirmCandidate(s.id, s.lat, s.lng, s.radius_m, s.label)
                            }
                          >
                            确认为围挡
                          </button>
                          {markingConfig && (
                            <button
                              type="button"
                              title="存成共享标注，附近的人分析时也能用上"
                              onClick={() =>
                                openComposer({
                                  type: 'closure',
                                  point: { lat: s.lat, lng: s.lng },
                                  radius: s.radius_m,
                                  source: 'recheck',
                                  context: `来自复测巡检 · ${s.label}`,
                                })
                              }
                            >
                              共享
                            </button>
                          )}
                          <button type="button" onClick={() => dismiss(s.id)}>
                            忽略
                          </button>
                        </footer>
                      </li>
                    ))}
                  </ul>
                )}
                {constructionResult && (
                  <p className="hint">
                    {constructionResult.candidates.length > 0
                      ? `工地候选 ${constructionResult.candidates.length} 处` +
                        `（压在步行路线上的排在前面）。`
                      : '工地检索没有找到候选。POI 召回有限，查不到不等于没有工地。'}
                    {constructionResult.failed_keywords.length > 0
                      ? `「${constructionResult.failed_keywords.join('、')}」检索失败。`
                      : ''}
                  </p>
                )}
                {sites.length > 0 && (
                  <ul className="candidate-list" aria-label="工地 POI 候选">
                    {sites.slice(0, 8).map((c) => {
                      const key = `poi-${c.lat}-${c.lng}`
                      return (
                        <li key={key}>
                          <div>
                            <b>{c.name}</b>
                            <span>
                              距中心 {c.distance_m} 米
                              {c.on_route ? ' · 在步行路线上' : ''}
                            </span>
                            {c.address && <p>{c.address}</p>}
                          </div>
                          <footer>
                            <button
                              type="button"
                              className="primary"
                              onClick={() =>
                                confirmCandidate(
                                  key,
                                  c.lat,
                                  c.lng,
                                  c.radius_m,
                                  `工地：${c.name}`.slice(0, 40),
                                )
                              }
                            >
                              确认为围挡
                            </button>
                            {markingConfig && (
                              <button
                                type="button"
                                title="存成共享标注，附近的人分析时也能用上"
                                onClick={() =>
                                  openComposer({
                                    type: 'closure',
                                    point: { lat: c.lat, lng: c.lng },
                                    radius: c.radius_m,
                                    source: 'poi',
                                    context: '来自工地检索',
                                  })
                                }
                              >
                                共享
                              </button>
                            )}
                            <button type="button" onClick={() => dismiss(key)}>
                              忽略
                            </button>
                          </footer>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </details>
              <label className="planning-closure-settings">
                假设围挡半径
                <select
                  value={closureRadius}
                  onChange={(e) => setClosureRadius(Number(e.target.value))}
                >
                  {[30, 50, 100, 200].map((r) => (
                    <option key={r} value={r}>
                      {r} 米
                    </option>
                  ))}
                </select>
              </label>
              {closures.length > 0 ? (
                <ul className="planning-closure-list">
                  {closures.map((c, i) => (
                    <li key={`${c.lat}-${c.lng}-${i}`}>
                      <span>
                        #{i + 1} · 半径 {c.radius_m} 米{c.label ? ` · ${c.label}` : ''}
                      </span>
                      <button
                        type="button"
                        aria-label={`删除第 ${i + 1} 处围挡`}
                        onClick={() => { planningRequest.current += 1; planningController.current?.abort(); setPlanningBusy(false); setClosurePreview(null); setClosures(closures.filter((_, j) => j !== i)) }}
                      >
                        删除
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="trip-empty">还没有假设围挡，在地图上点选位置。</p>
              )}
              {closurePreview && <div className="planning-comparison"><b>假设评分 {closurePreview.properties.planning_baseline?.report?.total ?? '—'} → {closurePreview.properties.report?.total ?? '—'}</b><p>可达面积 {closurePreview.properties.planning_baseline?.area_km2.toFixed(3) ?? '—'} → {closurePreview.properties.area_km2.toFixed(3)} km²</p><p>前后均按当前规则重算；正式报告与出行继续使用原始结果。</p><p>圈缩小可能移除原有缺失网格，评分升高不代表封路有益。</p></div>}
            </PlanningDrawer>
          )}
          {trip && isochrone && <TripDrawer key={trip.key} feature={isochrone} origin={trip.origin}
            initialCategory={trip.category} targetId={trip.targetId} selected={tripSelection} selectionKey={tripSelectionKey} picking={tripPicking}
            onSelect={selectTrip} onMapData={updateTripMap} onClose={closeTrip}
            hidden={Boolean(composer || guide || selectedMarkingId)} onGuide={openGuide}
            onAddMissing={category => openComposer({ type: 'facility_extra', source: 'user', category }, true)}
            onOriginChange={changeTripOrigin} onCancelPick={() => setTripPicking(false)}
            onPickOrigin={() => setTripPicking(true)} onResetOrigin={() => changeTripOrigin({ ...isochrone.properties.center!, kind: 'center' })} />}
          {guide && <TripGuide value={guide} onChange={updateGuide} onClose={closeGuide} />}
          {trip && tripPicking && !composer && !guide && <div className="trip-modebar floating" role="status"><span className="trip-pick-icon"><Icon name="pin" size={17} /></span><span>点击地图，设置新起点</span><button type="button" aria-label="取消选择起点" onClick={() => setTripPicking(false)}>取消</button></div>}
          {config?.browser_ak ? (
            <MapView
              ak={config.browser_ak}
              center={center}
              isochrone={trip ? isochrone : planningMode === 'closure' && closurePreview ? closurePreview : mapFeature}
              fitKey={isochrone}
              trip={composer ? null : tripMap}
              tripOpen={Boolean(trip && !composer)}
              guide={guide}
              onTripSelect={selectTrip}
              layers={layers}
              onLayers={setLayers}
              blindCategory={blindCategory}
              closures={closures}
              pickEnabled={
                composer
                  ? composerPicking
                  : Boolean(trip && tripPicking) || (!trip && (pickEnabled || comparePicking || placing != null || closurePlacing))
              }
              pickHint={
                trip && tripPicking
                  ? '点地图选择出行起点'
                  : closurePlacing
                  ? `点地图放一处假设围挡（半径 ${closureRadius} 米，不消耗配额）`
                  : placing
                    ? `点地图任意位置，放置「${placing}」`
                    : comparePicking
                      ? '点地图，把这里作为对比地点（消耗配额）'
                      : '点击地图将按新中心点重新计算（消耗配额）'
              }
              viewSwitch={switcher}
              resultView={resultView}
              onResultView={(view) => setViewEntry({ owner: isochrone, value: view })}
              onRerun={rerunWithMarkings}
              busy={busy}
              altRing={altRing}
              canMark={Boolean(markingConfig) && !placing && !closurePlacing && !comparePicking}
              onIntent={handleIntent}
              reveal={reveal}
              radiusLimits={markingConfig?.limits.closure_radius_m}
              onDraftRadius={(radius) =>
                draftHistory.push({ ...draftHistory.draft, radius })
              }
              simulation={planningMode === 'facility' ? simulation : null}
              compare={compareFeature}
              suspects={suspects}
              recheckRays={recheckRays}
              constructionSites={sites}
              markings={nearbyMarkings}
              selectedMarkingId={selectedMarkingId}
              onSelectMarking={revealMarking}
              draft={composer ? draftHistory.draft : null}
              onPickPlace={(place) => {
                // 只在「位置」这一步换选中的设施；填详情时误点地图上的设施不该改掉它
                if (!composerPicking) return
                draftHistory.push({
                  ...draftHistory.draft,
                  place,
                  point: { lat: place.lat, lng: place.lng },
                })
              }}
              focus={mapFocus}
              blindCategories={keyCategories}
              onBlindCategory={setBlindCategory}
              onPickCenter={(lat, lng) => {
                if (trip && tripPicking) { changeTripOrigin({ lat, lng, kind: 'map' }); return }
                // 正在画共享标注：这一下是给草图放点，不改中心、不算路
                if (composer) {
                  if (!composerPicking || !markingConfig) return
                  draftHistory.push(
                    applyMapClick(
                      draftHistory.draft,
                      { lat, lng },
                      coverage?.places ?? [],
                      markingConfig.limits.polygon_max_vertices,
                    ),
                  )
                  return
                }
                if (closurePlacing) {
                  addClosure(lat, lng)
                  return
                }
                if (placing) {
                  handleSimulate({
                    action: 'site',
                    category: placing,
                    title: `在此新建${placing}`,
                    reason: '地图任意点放置',
                    covers: 0,
                    lat,
                    lng,
                  })
                  return
                }
                if (comparePicking) runCompare(lat, lng)
                else run(lat, lng)
              }}
              onError={setError}
            />
          ) : (
            <div className="placeholder" role="status">
              {config
                ? (config.warnings?.find((w) => w.code === 'missing_browser_ak')?.message ??
                  '浏览器端 AK 为空，地图底图无法加载。报告与样例数据仍可在左侧查看。')
                : (error ?? '正在载入地图配置…')}
            </div>
          )}

          {busy && (
            <div className="progress-card floating" role="status">
              <div className="progress-head">
                <span>分析中</span>
                <button type="button" onClick={() => abortRef.current?.abort()}>
                  取消
                </button>
              </div>
              <p>{stageText ?? '准备中…'}</p>
              <ol>
                {(
                  [
                    ['isochrone', '等时圈'],
                    ['coverage', '设施'],
                    ['blindspots', '盲区'],
                    ['done', '报告'],
                  ] as const
                ).map(([id, label], i, all) => {
                  // 「叠加用户标注」是盲区判定的第二遍，进度上仍算在「盲区」这一格
                  const at = stageId === 'markings' ? 'blindspots' : stageId
                  const current = all.findIndex(([key]) => key === at)
                  const state = i < current ? 'done' : i === current ? 'active' : 'todo'
                  return (
                    <li key={id} className={state}>
                      {label}
                    </li>
                  )
                })}
              </ol>
            </div>
          )}

          {adminOpen && markingConfig && (
            <AdminReview
              config={markingConfig}
              onClose={() => setAdminOpen(false)}
              onLocate={locateMarking}
              onChanged={refreshMarkings}
            />
          )}
          {toast && <UndoToast key={toast.id} toast={toast} onDone={() => setToast(null)} />}
        </main>
      </div>
    </div>
  )
}
