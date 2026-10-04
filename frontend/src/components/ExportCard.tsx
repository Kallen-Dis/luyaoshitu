import { useState } from 'react'
import { ApiError, exportFeature } from '../api'
import type { IsochroneFeature } from '../types'

type Format = 'zip' | 'md' | 'geojson' | 'csv' | 'json'

const FORMATS: { format: Format; label: string }[] = [
  { format: 'zip', label: 'ZIP 全量打包' },
  { format: 'md', label: 'Markdown 报告' },
  { format: 'geojson', label: 'GeoJSON' },
  { format: 'csv', label: '盲区 CSV' },
  { format: 'json', label: 'JSON' },
]

interface Props {
  feature: IsochroneFeature
  /** 样例快照直接走 GET 链接，不必把整份结果再传回后端。 */
  sampleId: string | null
}

/**
 * 成果导出：样例、实时计算、历史记录都能导出。
 * 非样例结果把当前结果 POST 给 /api/export，后端按当前口径重算报告后打包，零 API 消耗。
 */
export function ExportCard({ feature, sampleId }: Props) {
  const [pending, setPending] = useState<Format | null>(null)
  const [error, setError] = useState<string | null>(null)
  const linkId = sampleId

  async function download(format: Format) {
    setPending(format)
    setError(null)
    try {
      const { blob, filename } = await exportFeature(feature, format)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      // 下载已交给浏览器，稍后再释放，避免个别浏览器还没开始读就被回收
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : '导出失败')
    } finally {
      setPending(null)
    }
  }

  const simulated = Boolean(feature.properties.simulated)

  return (
    <section className="card">
      <h2 className="card-title">成果导出</h2>
      <p className="hint">
        {sampleId ? '基于当前载入的快照生成' : '基于当前这次结果生成'}
        ，含综合结论、体检报告、灰色区域诊断与设施覆盖表，零 API 消耗。
        {simulated ? '离线模拟结果会在报告里注明「非真实路网」。' : ''}
      </p>
      <div className="export-row">
        {FORMATS.map(({ format, label }, i) =>
          linkId ? (
            <a
              key={format}
              className={i === 0 ? 'export-btn primary' : 'export-btn'}
              href={`/api/samples/${encodeURIComponent(linkId)}/export?format=${format}`}
            >
              {label}
            </a>
          ) : (
            <button
              key={format}
              type="button"
              className={i === 0 ? 'export-btn primary' : 'export-btn'}
              disabled={pending !== null}
              aria-busy={pending === format}
              onClick={() => void download(format)}
            >
              {pending === format ? '生成中…' : label}
            </button>
          ),
        )}
      </div>
      {error && (
        <p className="hint error-text" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
