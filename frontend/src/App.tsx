import { useCallback, useEffect, useState } from 'react'
import './App.css'
import { ApiError, computeIsochrone, fetchConfig, fetchSample, fetchSamples, geocode } from './api'
import { MapView } from './components/MapView'
import { MetricsPanel } from './components/MetricsPanel'
import { ReportCard } from './components/ReportCard'
import type { AppConfig, IsochroneFeature, SampleMeta } from './types'

function shortName(name: string) {
  return name.replace(/^上海市普陀区/, '')
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [samples, setSamples] = useState<SampleMeta[]>([])
  const [activeSampleId, setActiveSampleId] = useState<string | null>(null)
  const [center, setCenter] = useState({ lat: 31.247979, lng: 121.416775 })
  const [isochrone, setIsochrone] = useState<IsochroneFeature | null>(null)
  const [minutes, setMinutes] = useState(15)
  const [directions, setDirections] = useState(36)
  const [address, setAddress] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [showHeatmap, setShowHeatmap] = useState(true)
  const [showBlindspots, setShowBlindspots] = useState(true)
  const [withCoverage, setWithCoverage] = useState(true)
  // 默认关闭：演示时点地图极易误触发实时计算。地址搜索与「重新计算」仍是显式操作。
  const [pickEnabled, setPickEnabled] = useState(false)

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
  }, [])

  function applySample(id: string, feature: IsochroneFeature) {
    setActiveSampleId(id)
    setIsochrone(feature)
    setMinutes(feature.properties.minutes)
    if (feature.properties.center) setCenter(feature.properties.center)
    setPickEnabled(false)
    setNotice(`已载入预生成样例：${feature.properties.name ?? '未命名'}（不消耗 API 配额）`)
  }

  function loadSample(id: string) {
    return fetchSample(id)
      .then((feature) => applySample(id, feature))
      .catch((e: Error) => setError(e.message))
  }

  const run = useCallback(
    async (lat: number, lng: number) => {
      setBusy(true)
      setError(null)
      setNotice(null)
      setActiveSampleId(null)
      try {
        const feature = await computeIsochrone({
          lat,
          lng,
          minutes,
          directions,
          coverage: withCoverage,
          blindspots: withCoverage,
        })
        setIsochrone(feature)
        setCenter({ lat, lng })
      } catch (err) {
        if (err instanceof ApiError && err.code === 'quota_exhausted') {
          setError(`${err.message}（可继续查看左侧预生成样例）`)
        } else {
          setError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        setBusy(false)
      }
    },
    [minutes, directions, withCoverage],
  )

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

  return (
    <div className="layout">
      <aside className="sidebar">
        <header>
          <h1>路遥识途</h1>
          <p className="subtitle">基于百度地图真实路网的步行等时圈与民生设施盲区诊断</p>
        </header>

        <section>
          <h2>对比样例</h2>
          <p className="hint">载入快照不消耗配额。先看配套较全的曹杨，再看被路网切开的桃浦。</p>
          <div className="sample-list">
            {samples.map((s) => (
              <button
                key={s.id}
                className={activeSampleId === s.id ? 'sample active' : 'sample'}
                onClick={() => loadSample(s.id)}
              >
                <span className="sample-head">
                  <span className="sample-name">{shortName(s.name)}</span>
                  {s.grade && (
                    <span className={`sample-grade grade-${s.grade}`}>
                      {s.grade}
                      {s.total != null ? ` ${Math.round(s.total)}` : ''}
                    </span>
                  )}
                </span>
                <span className="sample-meta">
                  {s.area_ratio != null
                    ? `真实面积仅 ${(s.area_ratio * 100).toFixed(0)}%`
                    : `${s.area_km2.toFixed(2)} km²`}
                  {s.facilities_in != null && ` · 圈内 ${s.facilities_in} 处`}
                  {s.facilities_in === 0 &&
                    s.facilities_nearby != null &&
                    `（附近 ${s.facilities_nearby} 处走不到）`}
                </span>
              </button>
            ))}
            {samples.length === 0 && <p className="hint">暂无样例，请先运行 run_isochrone.py</p>}
          </div>
        </section>

        <section>
          <h2>图层</h2>
          <label className="check">
            <input
              type="checkbox"
              checked={showHeatmap}
              onChange={(e) => setShowHeatmap(e.target.checked)}
            />
            步行耗时热力图
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={showBlindspots}
              onChange={(e) => setShowBlindspots(e.target.checked)}
            />
            服务盲区点位
          </label>
        </section>

        <details className="live-panel">
          <summary>实时计算（消耗配额）</summary>
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

        {isochrone && <MetricsPanel props={isochrone.properties} />}
        {isochrone?.properties.report && (
          <ReportCard
            report={isochrone.properties.report}
            coverage={isochrone.properties.coverage}
            rays={isochrone.properties.rays}
          />
        )}
      </aside>

      <main className="stage">
        {config?.browser_ak ? (
          <MapView
            ak={config.browser_ak}
            center={center}
            isochrone={isochrone}
            showHeatmap={showHeatmap}
            showBlindspots={showBlindspots}
            pickEnabled={pickEnabled}
            onPickCenter={(lat, lng) => run(lat, lng)}
            onError={setError}
          />
        ) : (
          <div className="placeholder">{error ?? '正在载入地图配置…'}</div>
        )}
        {busy && <div className="busy-mask">正在按真实路网计算…</div>}
      </main>
    </div>
  )
}
