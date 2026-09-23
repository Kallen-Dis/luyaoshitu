import { wideCensus } from '../lib/wideBlind'
import type {
  Coverage,
  ExamReport,
  IsochroneProperties,
  Place,
  Prescription,
  RayMetric,
  SimulationResult,
} from '../types'
interface Props {
  report: ExamReport
  coverage?: Coverage
  rays?: RayMetric[]
  /** 等时圈原始属性，供图签栏展示可追溯信息。 */
  meta?: IsochroneProperties
  /** 15 分钟圈外环，[经度, 纬度]。有它才能把方格数和地图对齐。 */
  ring?: [number, number][]
  /** 模拟新建结果；有值时在自动诊疗卡片内展示前后对比。 */
  simulation?: SimulationResult | null
  onSimulate?: (p: Prescription) => void
  onClearSimulation?: () => void
}

const ACTION_LABEL: Record<string, string> = {
  connect: '打通',
  site: '补设',
  densify: '加密',
  network: '路网',
  maintain: '维持',
}

const READ_ORDER = ['生鲜采买', '医药', '基础教育', '基础医疗', '养老服务', '文体休闲']
const KEY_FACILITY = new Set(['生鲜采买', '医药', '基础教育'])

interface FacilityRead {
  category: string
  key: boolean
  inCircle: number
  nearest: { name: string; straightM: number; minutes: number; inside: boolean } | null
  cellsMissing: number | null
}

function walkMinutes(straightM: number, detour: number | null): number {
  const factor = detour && detour > 0 ? detour : 1
  return Math.round(((straightM * factor) / 1.2 / 60) * 10) / 10
}

function facilityReads(
  center: { lat: number; lng: number },
  places: Place[],
  detour: number | null,
  census: { total: number; missing: Record<string, number> } | null,
): FacilityRead[] {
  const present = new Set(places.map((place) => place.category))
  const names = [
    ...READ_ORDER.filter((name) => present.has(name)),
    ...[...present].filter((name) => !READ_ORDER.includes(name)),
  ]
  return names.map((category) => {
    const list = places.filter((place) => place.category === category)
    let nearest = list[0]
    let best = metersBetween(center, nearest)
    for (const place of list.slice(1)) {
      const dist = metersBetween(center, place)
      if (dist < best) {
        best = dist
        nearest = place
      }
    }
    return {
      category,
      key: KEY_FACILITY.has(category),
      inCircle: list.filter((place) => place.in_circle).length,
      nearest: nearest
        ? {
            name: nearest.name,
            straightM: best,
            minutes: walkMinutes(best, detour),
            inside: nearest.in_circle,
          }
        : null,
      cellsMissing: census && KEY_FACILITY.has(category) ? (census.missing[category] ?? 0) : null,
    }
  })
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

function FacilitySheet({
  reads,
  detour,
  census,
}: {
  reads: FacilityRead[]
  detour: number | null
  census: { total: number; blind: number } | null
}) {
  return (
    <>
      <p className="exam-lead">
        每一类都写出最近的一处，以及按
        {detour ? `本圈实测平均绕行 ${detour.toFixed(2)} 倍` : '直线 1.2 米/秒'}
        估算的步行时间。
        {census
          ? `菜场、药房、学校的方格与地图是同一套：周围 1.5 公里共 ${census.total} 格，其中 ${census.blind} 格缺至少一类。`
          : ''}
      </p>
      <ul className="exam-list">
        {reads.map((item) => (
          <li key={item.category}>
            <div className="exam-cat">
              <b>{item.category}</b>
              {item.nearest ? (
                <span className={item.nearest.inside ? 'exam-min in' : 'exam-min out'}>
                  {item.nearest.minutes}
                  <em>分钟</em>
                </span>
              ) : (
                <span className="exam-min out">
                  —<em>没有</em>
                </span>
              )}
            </div>
            <p>
              {item.nearest
                ? `最近「${item.nearest.name}」，直线 ${Math.round(item.nearest.straightM)} 米，估算步行 ${item.nearest.minutes} 分钟，${item.nearest.inside ? `已在 15 分钟圈内（圈内 ${item.inCircle} 处）` : '在 15 分钟圈外'}。`
                : '检索范围内没有这一类。'}
              {item.cellsMissing != null && census
                ? ` 地图上 ${item.cellsMissing} / ${census.total} 格，直线 1 公里内没有它。`
                : ''}
            </p>
          </li>
        ))}
      </ul>
    </>
  )
}

function sumCounts(table: Record<string, number> | null | undefined): number {
  if (!table) return 0
  return Object.values(table).reduce((acc, n) => acc + n, 0)
}

export function ReportCard({
  report,
  coverage,
  rays,
  meta,
  ring,
  simulation,
  onSimulate,
  onClearSimulation,
}: Props) {
  const nearbyCount = sumCounts(coverage?.nearby_categories)
  const places = coverage?.places ?? []
  const center = meta?.center
  const census =
    center && ring && ring.length >= 3 && places.length > 0
      ? wideCensus(center, places, ring)
      : null
  const reads = center && places.length > 0 ? facilityReads(center, places, meta?.mean_detour ?? null, census) : null

  // 覆盖对比条的共同量尺：以各品类「附近」最大值归一，圈内/附近可直接比长短
  const coverEntries = Object.entries(report.categories ?? {})
  const maxCover = Math.max(
    1,
    ...coverEntries.map(([name, count]) =>
      Math.max(count, coverage?.nearby_categories?.[name] ?? count),
    ),
  )

  return (
    <>
      {report.coverage_pending && (
        <p className="hint card-inline-hint">本次只算了等时圈，未做设施覆盖与盲区判定。</p>
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
                {onSimulate && p.lat != null && p.lng != null && p.category && (
                  <button type="button" className="sim-btn" onClick={() => onSimulate(p)}>
                    模拟在此新建「{p.category}」
                  </button>
                )}
              </li>
            ))}
          </ol>

          {simulation && (
            <div className="sim-result">
              <div className="sim-head">
                <span className="sim-tag">模拟新建 · {simulation.category}</span>
                {onClearSimulation && (
                  <button type="button" className="sim-clear" onClick={onClearSimulation}>
                    清除模拟
                  </button>
                )}
              </div>
              <ul className="sim-metrics">
                <li>
                  <span>消除盲区网格</span>
                  <b>{simulation.covered_count} 个</b>
                </li>
                <li>
                  <span>盲区网格总数</span>
                  <b>
                    {simulation.before.blind_count ?? '—'} → {simulation.after.blind_count ?? '—'}
                  </b>
                </li>
                <li>
                  <span>体检总分</span>
                  <b>
                    {simulation.before.score ?? '—'} → {simulation.after.score ?? '—'}
                    {simulation.after.grade ? `（${simulation.after.grade}）` : ''}
                  </b>
                </li>
                {simulation.before.blind_ratio?.[simulation.category] != null && (
                  <li>
                    <span>{simulation.category}不可达占比</span>
                    <b>
                      {((simulation.before.blind_ratio[simulation.category] ?? 0) * 100).toFixed(
                        1,
                      )}
                      % →{' '}
                      {((simulation.after.blind_ratio?.[simulation.category] ?? 0) * 100).toFixed(
                        1,
                      )}
                      %
                    </b>
                  </li>
                )}
              </ul>
              <p className="hint">{simulation.approximation}</p>
            </div>
          )}
        </section>
      )}

      <section className="card">
        <h2 className="card-title">设施覆盖与盲区</h2>
        {reads ? (
          <FacilitySheet reads={reads} detour={meta?.mean_detour ?? null} census={census} />
        ) : report.categories ? (
          <>
            <ul className="cover-bars">
              {coverEntries.map(([name, count]) => {
                const nearby = Math.max(count, coverage?.nearby_categories?.[name] ?? count)
                return (
                  <li key={name} className={count === 0 ? 'blind' : ''}>
                    <span className="cover-name">{name}</span>
                    <span className="cover-track">
                      <i className="cover-out" style={{ width: `${(nearby / maxCover) * 100}%` }} />
                      <i className="cover-in" style={{ width: `${(count / maxCover) * 100}%` }} />
                    </span>
                    <span className="cover-num">
                      {count === 0 ? '缺失' : count}
                      {nearby > count && <em className="cover-out-num"> / {nearby}</em>}
                    </span>
                  </li>
                )
              })}
              {report.failed_categories.map((name) => (
                <li key={name}>
                  <span className="cover-name">{name}</span>
                  <span className="cover-track" />
                  <span className="cover-num cover-unknown">查询失败</span>
                </li>
              ))}
            </ul>
            {nearbyCount > 0 && (
              <p className="cover-legend">
                <span>
                  <i className="cover-in" />
                  圈内
                </span>
                <span>
                  <i className="cover-out" />
                  检索半径内（含圈外）
                </span>
              </p>
            )}
          </>
        ) : null}

        {report.failed_categories.length > 0 && (
          <p className="hint">
            查询失败的品类数量未知，未计入评分——把「查不到」当成「真的没有」，
            会让接口故障伪装成服务盲区。
          </p>
        )}

        {!reads && report.blinds.length > 0 && (
          <div className="blinds">
            <h3>圈内完全缺失</h3>
            <p>整个等时圈内未检索到：{report.blinds.join('、')}</p>
          </div>
        )}

        {!reads && report.blind_ratio && Object.keys(report.blind_ratio).length > 0 && (
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

      {meta && (
        <section className="card signature">
          <dl className="sig-grid">
            <div>
              <dt>图名</dt>
              <dd>{meta.minutes} 分钟生活圈体检报告</dd>
            </div>
            <div>
              <dt>中心点</dt>
              <dd>
                {meta.center
                  ? `${meta.center.lat.toFixed(5)}, ${meta.center.lng.toFixed(5)}`
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>坐标系</dt>
              <dd>
                {meta.input_coord_sys && meta.input_coord_sys !== 'bd09'
                  ? `BD-09（${meta.input_coord_sys.toUpperCase()} 输入）`
                  : 'BD-09（百度）'}
              </dd>
            </div>
            <div>
              <dt>出行方式</dt>
              <dd>{meta.mode_label ?? '步行'}</dd>
            </div>
            <div>
              <dt>采样</dt>
              <dd>
                {meta.sampled_points} 点 · {rays?.length ?? 0} 方向
              </dd>
            </div>
            <div>
              <dt>数据来源</dt>
              <dd>
                {meta.simulated
                  ? '离线模拟（非真实路网）'
                  : report.coverage_source ?? '实时计算'}
              </dd>
            </div>
            <div>
              <dt>生成时间</dt>
              <dd>
                {meta.generated_at
                  ? new Date(meta.generated_at).toLocaleString('zh-CN', { hour12: false })
                  : '实时计算'}
              </dd>
            </div>
            <div>
              <dt>出图</dt>
              <dd>路遥识途 · 真实路网口径</dd>
            </div>
          </dl>
        </section>
      )}
    </>
  )
}
