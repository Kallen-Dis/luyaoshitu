import type { Coverage, ExamReport, RayMetric } from '../types'
import { DirectionRadar } from './DirectionRadar'
import { ScoreGauge } from './ScoreGauge'

interface Props {
  report: ExamReport
  coverage?: Coverage
  rays?: RayMetric[]
}

const ACTION_LABEL: Record<string, string> = {
  connect: '打通',
  site: '补设',
  densify: '加密',
  network: '路网',
  maintain: '维持',
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

/** 进度条按分段着色：满屏同色的条形看不出哪一项才是短板。 */
function barLevel(value: number): string {
  if (value >= 75) return 'high'
  if (value >= 55) return 'mid'
  return 'low'
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
    <>
      <section className="card">
        <h2 className="card-title">体检报告</h2>

        <div className="report-score">
          <ScoreGauge total={report.total} grade={report.grade} />
          <div className="score-side">
            <div className="grade-line">
              <span className={`report-grade grade-${report.grade}`}>{report.grade}</span>
              <span className="grade-word">
                {report.straight_inflation != null
                  ? `直线口径高估 ${report.straight_inflation.toFixed(1)} 倍`
                  : '真实路网口径'}
              </span>
            </div>
            {coverage && (
              <p className={allMissing ? 'story story-alert' : 'story'}>
                {allMissing
                  ? `真实 15 分钟圈内设施 ${inCount} 处，检索半径内却有 ${nearbyCount} 处——直线看得见，路网走不到。`
                  : `圈内 ${inCount} 处民生设施${
                      outside > 0 ? `，另有 ${outside} 处在附近但走不进等时圈` : ''
                    }。`}
              </p>
            )}
          </div>
        </div>

        <ul className="dim-list">
          {dims.map(([key, value]) => (
            <li key={key}>
              <span>{DIM_LABEL[key]}</span>
              {value == null ? (
                <em className="pending">待采集</em>
              ) : (
                <span className="dim-bar">
                  <i className={barLevel(value)} style={{ width: `${value}%` }} />
                </span>
              )}
              {value != null && <b className="dim-value">{value.toFixed(0)}</b>}
            </li>
          ))}
        </ul>

        {report.coverage_pending && (
          <p className="hint">本次只算了等时圈，未做设施覆盖与盲区判定。</p>
        )}
        {report.coverage_source && <p className="hint">{report.coverage_source}</p>}
      </section>

      {rays && rays.length > 0 && (
        <section className="card">
          <h2 className="card-title">各方向可达半径</h2>
          <DirectionRadar rays={rays} />
          <p className="radar-caption">红点为算路失败截断的方向，即真实屏障所在</p>
        </section>
      )}

      {report.prescriptions && report.prescriptions.length > 0 && (
        <section className="card">
          <h2 className="card-title">自动诊疗</h2>
          <p className="hint">
            由盲区网格与圈内外设施对照生成，不另耗配额。圈外有、圈内无则优先打通路网。
          </p>
          <ol className="plan-list">
            {report.prescriptions.map((p) => (
              <li
                key={`${p.action}-${p.category}-${p.lat}-${p.lng}`}
                className={`action-${p.action}`}
              >
                <div className="plan-head">
                  <span className={`plan-tag action-${p.action}`}>{ACTION_LABEL[p.action]}</span>
                  {p.category && <span>{p.category}</span>}
                  {p.covers > 0 && <span className="plan-covers">覆盖 {p.covers} 个居民点</span>}
                </div>
                <strong>{p.title}</strong>
                <p>{p.reason}</p>
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="card">
        <h2 className="card-title">设施覆盖与盲区</h2>

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

        {report.blinds.length > 0 && (
          <div className="blinds">
            <h3>圈内完全缺失</h3>
            <p>整个等时圈内未检索到：{report.blinds.join('、')}</p>
          </div>
        )}

        {report.blind_ratio && Object.keys(report.blind_ratio).length > 0 && (
          <div className="blinds grid">
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
                  </span>
                  <b className="ratio-value">{(ratio * 100).toFixed(0)}%</b>
                </li>
              ))}
            </ul>
            <p className="hint">
              占比为该品类步行 1 公里不可达的网格比例，测距失败的网格不计入。
            </p>
          </div>
        )}
      </section>
    </>
  )
}
