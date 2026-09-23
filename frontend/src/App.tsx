import { useCallback, useEffect, useRef, useState } from 'react'
import './App.css'
import { ApiError, computeIsochroneStream, fetchConfig, fetchDemo, fetchHistories, fetchHistory, fetchSample, fetchSamples, geocode, simulate } from './api'
import { wideBlindCells, wideCensus } from './lib/wideBlind'
import { CompareCard } from './components/CompareCard'
import { DirectionRadar } from './components/DirectionRadar'
import { MapView } from './components/MapView'
import { ReportCard } from './components/ReportCard'
import type { AppConfig, HistoryMeta, IsochroneFeature, Prescription, SampleMeta, SimulationResult } from './types'

function erasedCount(
  feature: IsochroneFeature | null,
  simulation: SimulationResult,
  category: string,
): number {
  const center = feature?.properties.center
  const ring = feature?.geometry.coordinates[0]
  if (!center || !ring) return 0
  const base = feature?.properties.coverage?.places ?? []
  const virt = {
    category: simulation.category,
    name: '拟建',
    lat: simulation.lat,
    lng: simulation.lng,
    in_circle: true,
  }
  const keep = (cells: { missing: string[] }[]) =>
    category === 'all' ? cells : cells.filter((cell) => cell.missing.includes(category))
  const before = keep(wideBlindCells(center, base, ring)).length
  const after = keep(wideBlindCells(center, [...base, virt], ring)).length
  return Math.max(0, before - after)
}

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
  const missing =
    report.blinds.length > 0 && nearbyTotal > 0
      ? `从这个落点步行 15 分钟走不到${report.blinds.join('、')}。周围 ${((props.coverage?.radius_m ?? 0) / 1000).toFixed(1)} 公里内有 ${nearbyTotal} 处${nearest ? `，最近的「${nearest.name}」直线 ${Math.round(nearest.meters)} 米，仍在圈外` : ''}。`
      : report.blinds.length > 0
        ? `从这个落点步行 15 分钟走不到${report.blinds.join('、')}。`
        : `圈内 ${inCount ?? 0} 处民生设施，六个品类都找得到。`
  const ring = feature.geometry.coordinates[0] ?? []
  const census =
    props.center && places.length > 0 && ring.length >= 3
      ? wideCensus(props.center, places, ring)
      : null
  const lines = [
    `图上是「${shortName(props.name ?? '当前地点')}」地名落点的 15 分钟步行范围，${reach}，面积 ${props.area_km2.toFixed(2)} km²。`,
    missing,
    census
      ? `地图上 ${census.blind} / ${census.total} 个方格，在周围 1.5 公里内、直线 1 公里到不了菜场、药房或学校。`
      : props.blindspots
        ? `${props.blindspots.blind_count} / ${props.blindspots.cell_count} 个网格，步行 1 公里内缺关键设施。`
        : '还没有做网格盲区判定。',
  ]
  return (
    <section className="card verdict">
      <p className="verdict-place">{shortName(props.name ?? '当前地点')}</p>
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
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showHeatmap, setShowHeatmap] = useState(false)
  const [showOutsidePlaces, setShowOutsidePlaces] = useState(true)
  const [showBlindspots, setShowBlindspots] = useState(true)
  const [withCoverage, setWithCoverage] = useState(true)
  const [pickEnabled, setPickEnabled] = useState(false)
  const [mode, setMode] = useState('walk')
  const [coordSys, setCoordSys] = useState('bd09')
  const [dataMode, setDataMode] = useState<'real' | 'demo'>('real')
  const [simulation, setSimulation] = useState<SimulationResult | null>(null)
  const [compareFeature, setCompareFeature] = useState<IsochroneFeature | null>(null)
  const [compareId, setCompareId] = useState<string | null>(null)
  const [comparePicking, setComparePicking] = useState(false)
  const [placing, setPlacing] = useState<string | null>(null)
  const [samplesOpen, setSamplesOpen] = useState(false)
  const [latText, setLatText] = useState('31.247979')
  const [lngText, setLngText] = useState('121.416775')
  const [blindCategory, setBlindCategory] = useState('all')

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
        setPickEnabled(false)
        setNotice(`已载入历史记录 #${id}（不消耗 API 配额）`)
      })
      .catch((e: Error) => setError(e.message))
  }

  const run = useCallback(
    async (lat: number, lng: number) => {
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
            coord_sys: coordSys,
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
        if (err instanceof ApiError && err.code === 'quota_exhausted') {
          setError(`${err.message}（可继续查看右侧预生成样例）`)
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
    [minutes, directions, withCoverage, mode, coordSys, dataMode],
  )

  async function onMapSearch() {
    const query = address.trim()
    if (!query || busy) return
    setError(null)
    try {
      const hit = await geocode(query)
      if (comparePicking) await runCompare(hit.lat, hit.lng)
      else await run(hit.lat, hit.lng)
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
      await run(hit.lat, hit.lng)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setBusy(false)
    }
  }

  /** 进入对比：下一步用搜索、样例或点地图指定地点 B，可以是新地址。 */
  function toggleCompare() {
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
            coord_sys: coordSys,
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
        if (err instanceof ApiError && err.code === 'quota_exhausted') {
          setError('配额不够，没法为这个新地点做实时体检。可以改选预生成样例做对比。')
          return
        }
        setError(err instanceof Error ? err.message : String(err))
      } finally {
        if (abortRef.current === ctrl) {
          setBusy(false)
          setStageId('done')
        }
      }
    },
    [minutes, directions, withCoverage, mode, coordSys, dataMode],
  )

  /** 模拟新建：在处方点位放一处设施，后端本地重算盲区与分数（零 API）。 */
  function handleSimulate(p: Prescription) {
    if (!isochrone || p.lat == null || p.lng == null || !p.category) return
    setPlacing(p.category)
    simulate({ category: p.category, lat: p.lat, lng: p.lng, feature: isochrone })
      .then((result) => {
        setSimulation(result)
        setNotice(
          `模拟新建「${result.category}」：1 公里虚线圈内缺这一类的方格已消去，` +
            `总分 ${result.before.score ?? '-'} → ${result.after.score ?? '-'}（直线估算，零消耗）`,
        )
      })
      .catch((err: Error) => setError(err.message))
  }

  // 品类词表由后端下发，图层筛选与判定口径因此不会各写一份而对不上
  const keyCategories = (config?.categories ?? [])
    .filter((c) => c.key_facility)
    .map((c) => c.name)
  const props = isochrone?.properties
  const report = props?.report
  const coverage = props?.coverage

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">途</span>
          <div className="brand-text">
            <h1>路遥识途</h1>
            <p>基于百度地图真实路网的 15 分钟生活圈体检与规划助手</p>
          </div>
        </div>
        <div className="topbar-spacer" />
        <span
          className={
            activeSampleId
              ? 'source-chip snapshot'
              : props?.simulated
                ? 'source-chip demo'
                : 'source-chip live'
          }
        >
          <i />
          {activeSampleId
            ? '预生成快照 · 零 API 消耗'
            : props?.simulated
              ? '离线模拟 · 非真实路网数据'
              : '实时计算结果'}
        </span>
      </header>

      <div className="layout">
        <aside className="sidebar">
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
                    if (Number.isFinite(la) && Number.isFinite(ln)) run(la, ln)
                  }}
                >
                  计算
                </button>
              </div>
              <p className="hint">
                地图点选与地址搜索会自动回填（BD09）。手动输入时请按上方选择的坐标系。
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
                方向越多轮廓越精细。每 100 个采样点消耗 1 次批量算路请求，
                当前设置约 {Math.ceil((directions * 7) / 100)} 次。
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
                checked={pickEnabled}
                onChange={(e) => setPickEnabled(e.target.checked)}
              />
              允许点击地图重新计算
            </label>
            <p className="hint">默认关闭。误点一次就会烧掉一批算路点对。</p>

            <button className="primary" onClick={() => run(center.lat, center.lng)} disabled={busy}>
              {busy ? '计算中…' : '重新计算当前中心点'}
            </button>
          </details>

          {error && <div className="banner error">{error}</div>}
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
          {report && (
            <ReportCard
              report={report}
              coverage={coverage}
              rays={props?.rays}
              meta={props}
              ring={isochrone.geometry.coordinates[0]}
              simulation={simulation}
              onSimulate={handleSimulate}
              onClearSimulation={() => setSimulation(null)}
            />
          )}

          {activeSampleId && (
            <section className="card">
              <h2 className="card-title">成果导出</h2>
              <p className="hint">基于当前载入的快照生成，零 API 消耗。</p>
              <div className="export-row">
                <a
                  className="export-btn primary"
                  href={`/api/samples/${activeSampleId}/export?format=zip`}
                >
                  ZIP 全量打包
                </a>
                <a
                  className="export-btn"
                  href={`/api/samples/${activeSampleId}/export?format=md`}
                >
                  Markdown 报告
                </a>
                <a
                  className="export-btn"
                  href={`/api/samples/${activeSampleId}/export?format=geojson`}
                >
                  GeoJSON
                </a>
                <a
                  className="export-btn"
                  href={`/api/samples/${activeSampleId}/export?format=csv`}
                >
                  盲区 CSV
                </a>
                <a
                  className="export-btn"
                  href={`/api/samples/${activeSampleId}/export?format=json`}
                >
                  JSON
                </a>
              </div>
            </section>
          )}
        </aside>

        <main className="stage">
          <div className={comparePicking ? 'map-search compare' : 'map-search'}>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                void onMapSearch()
              }}
            >
              {comparePicking && <span className="search-tag">对比地点</span>}
              <input
                value={address}
                placeholder={
                  comparePicking
                    ? '输入要对比的小区或地址'
                    : '输入地址或小区名，或从样例打开'
                }
                onChange={(e) => setAddress(e.target.value)}
              />
              <button type="submit" disabled={busy || !address.trim()}>
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
          <div className="map-tools floating">
            <button
              type="button"
              className={compareFeature || comparePicking ? 'tool on' : 'tool'}
              onClick={toggleCompare}
            >
              {compareFeature || comparePicking ? '退出对比' : '对比'}
            </button>
            <button
              type="button"
              className={placing || simulation ? 'tool on' : 'tool'}
              onClick={() => {
                if (placing || simulation) {
                  setPlacing(null)
                  setSimulation(null)
                  return
                }
                setComparePicking(false)
                setPlacing(keyCategories[0] ?? '生鲜采买')
              }}
            >
              {placing || simulation ? '退出模拟' : '模拟新建'}
            </button>
            <button type="button" className="tool" onClick={() => window.print()}>
              导出 PDF
            </button>
          </div>
          {placing && (
            <section className="place-panel floating" aria-label="新建设施模拟">
              <header className="place-head">
                <div>
                  <p className="place-kicker">规划模拟</p>
                  <strong>假如在这里新建一处…</strong>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setPlacing(null)
                    setSimulation(null)
                  }}
                >
                  关闭
                </button>
              </header>
              <div className="mode-pills">
                {(keyCategories.length ? keyCategories : ['生鲜采买', '医药', '基础教育']).map(
                  (name) => (
                    <button
                      key={name}
                      type="button"
                      className={placing === name ? 'pill active' : 'pill'}
                      onClick={() => setPlacing(name)}
                    >
                      {name}
                    </button>
                  ),
                )}
              </div>
              <p>
                {placing
                  ? `放置模式：在地图上点一下，放一处${placing}。再点别的位置会换到新地点。`
                  : '先选类别，再在地图上点一下。'}
              </p>
              {simulation ? (
                <p className="place-diff">
                  {erasedCount(isochrone, simulation, blindCategory)} 个方格已从地图上消去。分数{' '}
                  {simulation.before.score ?? '—'} → {simulation.after.score ?? '—'}
                  {simulation.after.grade ? `（${simulation.after.grade}）` : ''}
                </p>
              ) : (
                <p>还没放下。点地图之后，这里显示前后分数。</p>
              )}
              <footer>
                <button type="button" disabled={!simulation} onClick={() => setSimulation(null)}>
                  撤销
                </button>
              </footer>
            </section>
          )}
          {config?.browser_ak ? (
            <MapView
              ak={config.browser_ak}
              center={center}
              isochrone={isochrone}
              showHeatmap={showHeatmap}
              showBlindspots={showBlindspots}
              blindCategory={blindCategory}
              pickEnabled={pickEnabled || comparePicking || placing != null}
              pickHint={
                placing
                  ? `点地图任意位置，放置「${placing}」`
                  : comparePicking
                    ? '点地图，把这里作为对比地点（消耗配额）'
                    : '点击地图将按新中心点重新计算（消耗配额）'
              }
              simulation={simulation}
              compare={compareFeature}
              onToggleHeatmap={() => setShowHeatmap((v) => !v)}
              onToggleBlindspots={() => setShowBlindspots((v) => !v)}
              blindCategories={keyCategories}
              onBlindCategory={setBlindCategory}
              showOutsidePlaces={showOutsidePlaces}
              onToggleOutsidePlaces={() => setShowOutsidePlaces((v) => !v)}
              onPickCenter={(lat, lng) => {
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
            <div className="placeholder">{error ?? '正在载入地图配置…'}</div>
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
                  const current = all.findIndex(([key]) => key === stageId)
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
        </main>
      </div>
    </div>
  )
}
