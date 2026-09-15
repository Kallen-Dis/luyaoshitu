import { useCallback, useEffect, useState } from 'react'
import './App.css'
import { ApiError, computeIsochrone, fetchConfig, fetchSample, fetchSamples, geocode } from './api'
import { MapView } from './components/MapView'
import { MetricsPanel } from './components/MetricsPanel'
import { ReportCard } from './components/ReportCard'
import type { AppConfig, IsochroneFeature, SampleMeta } from './types'

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [samples, setSamples] = useState<SampleMeta[]>([])
  const [center, setCenter] = useState({ lat: 31.284817, lng: 121.369523 })
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

  // 启动时载入配置与样例列表，并默认展示第一个样例。
  // 默认走预生成快照而不是实时计算，是为了让首屏不消耗任何 API 配额。
  useEffect(() => {
    Promise.all([fetchConfig(), fetchSamples()])
      .then(([cfg, list]) => {
        setConfig(cfg)
        setSamples(list)
        if (list.length > 0) return fetchSample(list[0].id).then(applySample)
      })
      .catch((err: Error) => setError(err.message))
  }, [])

  function applySample(feature: IsochroneFeature) {
    setIsochrone(feature)
    setMinutes(feature.properties.minutes)
    if (feature.properties.center) setCenter(feature.properties.center)
    setNotice(`已载入预生成样例：${feature.properties.name ?? '未命名'}（不消耗 API 配额）`)
  }

  const run = useCallback(
    async (lat: number, lng: number) => {
      setBusy(true)
      setError(null)
      setNotice(null)
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
          setError(`${err.message}（可继续查看下方预生成样例）`)
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
          <h2>预生成样例</h2>
          <p className="hint">载入快照不消耗 API 配额，适合快速演示。</p>
          <div className="sample-list">
            {samples.map((s) => (
              <button
                key={s.id}
                className={isochrone?.properties.name === s.name ? 'sample active' : 'sample'}
                onClick={() => fetchSample(s.id).then(applySample).catch((e) => setError(e.message))}
              >
                <span className="sample-name">{s.name}</span>
                <span className="sample-meta">
                  {s.area_km2.toFixed(2)} km² · 紧凑度 {s.compactness.toFixed(2)}
                </span>
              </button>
            ))}
            {samples.length === 0 && <p className="hint">暂无样例，请先运行 run_isochrone.py</p>}
          </div>
        </section>

        <section>
          <h2>实时计算</h2>
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
          <p className="hint">
            关闭后只算等时圈，不消耗地点检索配额，出分也只含路网三项。
          </p>

          <button className="primary" onClick={() => run(center.lat, center.lng)} disabled={busy}>
            {busy ? '计算中…' : '重新计算当前中心点'}
          </button>
          <p className="hint">也可直接在地图上点击选取中心点。</p>
        </section>

        {isochrone?.properties.blindspots && (
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
        )}

        {error && <div className="banner error">{error}</div>}
        {notice && !error && <div className="banner notice">{notice}</div>}

        {isochrone && <MetricsPanel props={isochrone.properties} />}
        {isochrone?.properties.report && (
          <ReportCard report={isochrone.properties.report} rays={isochrone.properties.rays} />
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
            onPickCenter={(lat, lng) => run(lat, lng)}
            onError={setError}
          />
        ) : (
          <div className="placeholder">
            {error ?? '正在载入地图配置…'}
          </div>
        )}
      </main>
    </div>
  )
}
