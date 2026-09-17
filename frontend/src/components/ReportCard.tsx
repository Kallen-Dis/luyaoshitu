import type { Coverage, ExamReport, RayMetric } from '../types'
import { DirectionRadar } from './DirectionRadar'

interface Props {
  report: ExamReport
  coverage?: Coverage
  rays?: RayMetric[]
}

const DIM_LABEL: Record<string, string> = {
  reach: '路网可达',
  compact: '方向均衡',
  detour: '路径效率',
  cover: '设施覆盖',
  equity: '可达均衡',
}

function sumCounts(table: Record<string, number> | null | undefined): number {
  if (!table) return 0
  return Object.values(table).reduce((acc, n) => acc + n, 0)
}

export function ReportCard({ report, coverage, rays }: Props) {
  const dims = [
    ['reach', report.dimensions.reach],
    ['compact', report.dimensions.compact],
    ['detour', report.dimensions.detour],
    ['cover', report.dimensions.cover],
    ['equity', report.dimensions.equity],
  ] as const

  const inCount = sumCounts(coverage?.categories ?? report.categories)
  const nearbyCount = sumCounts(coverage?.nearby_categories)
  const outside = Math.max(0, nearbyCount - inCount)
  const allMissing = report.blinds.length > 0 && inCount === 0

  return (
    <section className="report">
      <h2>体检报告</h2>
      <div className="report-score">
        <span className="report-total">{report.total.toFixed(0)}</span>
        <span className={`report-grade grade-${report.grade}`}>{report.grade}</span>
      </div>

      {report.straight_inflation != null && (
        <div className="callout">
          直线画圆会把可达面积算成 {report.ideal_area_km2.toFixed(2)} km²，
          高估 <strong>{report.straight_inflation.toFixed(1)} 倍</strong>
          （真实只有其 {(report.area_ratio * 100).toFixed(0)}%）。
        </div>
      )}

      {coverage && (
        <p className={allMissing ? 'story story-alert' : 'story'}>
          {allMissing
            ? `真实 15 分钟圈内设施 ${inCount} 处，检索半径内却有 ${nearbyCount} 处——直线看得见，路网走不到。`
            : `圈内 ${inCount} 处民生设施${
                outside > 0 ? `，另有 ${outside} 处在附近但走不进等时圈` : ''
              }。`}
        </p>
      )}

      <ul className="dim-list">
        {dims.map(([key, value]) => (
          <li key={key}>
            <span>{DIM_LABEL[key]}</span>
            {value == null ? (
              <em className="pending">待采集</em>
            ) : (
              <span className="dim-bar">
                <i style={{ width: `${value}%` }} />
                <b>{value.toFixed(0)}</b>
              </span>
            )}
          </li>
        ))}
      </ul>

      {rays && rays.length > 0 && <DirectionRadar rays={rays} />}

      {report.blinds.length > 0 && (
        <div className="blinds">
          <h3>圈内完全缺失</h3>
          <p>整个等时圈内未检索到：{report.blinds.join('、')}</p>
        </div>
      )}

      {report.blind_ratio && Object.keys(report.blind_ratio).length > 0 && (
        <div className="blinds">
          <h3>网格盲区</h3>
          <p>
            {report.blind_cell_count} / {report.cell_count} 个居民点步行 1 公里内
            至少缺一类关键设施。
          </p>
          <ul className="ratio-list">
            {Object.entries(report.blind_ratio).map(([name, ratio]) => (
              <li key={name}>
                <span>{name}</span>
                <span className="ratio-bar">
                  <i style={{ width: `${ratio * 100}%` }} />
                  <b>{(ratio * 100).toFixed(0)}%</b>
                </span>
              </li>
            ))}
          </ul>
          <p className="hint">占比为该品类步行 1 公里不可达的网格比例，测距失败的网格不计入。</p>
        </div>
      )}

      {report.categories && (
        <dl className="cover-grid">
          {Object.entries(report.categories).map(([name, count]) => (
            <div key={name} className={count === 0 ? 'blind' : ''}>
              <dt>{name}</dt>
              <dd>{count === 0 ? '缺失' : `${count} 处`}</dd>
            </div>
          ))}
          {report.failed_categories.map((name) => (
            <div key={name} className="unknown">
              <dt>{name}</dt>
              <dd>查询失败</dd>
            </div>
          ))}
        </dl>
      )}

      {report.failed_categories.length > 0 && (
        <p className="hint">
          查询失败的品类数量未知，未计入评分——把「查不到」当成「真的没有」，
          会让接口故障伪装成服务盲区。
        </p>
      )}

      {report.coverage_pending && (
        <p className="hint">本次只算了等时圈，未做设施覆盖与盲区判定。</p>
      )}

      {report.coverage_source && <p className="hint">{report.coverage_source}</p>}
    </section>
  )
}
