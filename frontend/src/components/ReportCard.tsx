import { freshEligible } from '../lib/fresh'
import { gridCensus } from '../lib/grid'
import { CrosscheckPanel } from './CrosscheckPanel'
import { ScoreRadar } from './ScoreRadar'
import { PLACE_MARK } from './map/context'
import type {
  Coverage,
  CrosscheckResult,
  CrosscheckSuspect,
  ExamReport,
  IsochroneProperties,
  Place,
  Prescription,
  SimulationResult,
  SitePlanResult,
} from '../types'
interface Props {
  onTrip?: (category: string) => void
  report: ExamReport
  coverage?: Coverage
  /** 等时圈原始属性：过街等待、网格、质量等读数都从这里取。 */
  meta?: IsochroneProperties
  /** 模拟新建结果；有值时在自动诊疗卡片内展示前后对比。 */
  simulation?: SimulationResult | null
  onSimulate?: (p: Prescription) => void
  onClearSimulation?: () => void
  /** 核验选址：对某一品类的补设处方做路网复核 */
  onVerifySite?: (category: string) => void
  sitePlan?: SitePlanResult | null
  /** 正在核验的品类 */
  sitePlanBusy?: string | null
  /** AI 二次核对（Agent Plan）的结果；onCrosscheck 为空表示不可用（没配 Token 或离线模拟） */
  crosscheck?: CrosscheckResult | null
  crosscheckBusy?: boolean
  onCrosscheck?: () => void
  onSimulateSuspect?: (s: CrosscheckSuspect) => void
  onShareSuspect?: (s: CrosscheckSuspect) => void
}

const STATUS_LABEL: Record<string, string> = {
  verified: '路网实测',
  estimated: '直线估算',
  over_budget: '超出点对预算，未核验',
}

/** 处方右上角的效果读数：打通是估算的「能消去几格」，补设是估算的「能覆盖几个缺口格」。 */
function coversText(p: Prescription): string | null {
  if (p.covers <= 0) return null
  if (p.action === 'connect' && p.basis) return `估算打通后消去 ${p.covers} 格`
  if (p.action === 'site' && p.basis) return `估算覆盖 ${p.covers} 个缺口格`
  return `覆盖 ${p.covers} 个居民点`
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
  cellsUnknown: number
}

/**
 * 从中心走到最近设施的估算分钟：直线 × 本圈平均绕行 ÷ 批量算路步速，再加每公里过街等待。
 * 这只是报告里的参考读数；盲区判定用的是逐格实测的步行距离。
 */
function walkMinutes(
  straightM: number,
  detour: number | null,
  speed: number,
  delayPerKmS: number | null,
): number {
  const factor = detour && detour > 0 ? detour : 1
  const walkM = straightM * factor
  const seconds = walkM / speed + ((delayPerKmS ?? 0) * walkM) / 1000
  return Math.round((seconds / 60) * 10) / 10
}

function facilityReads(
  center: { lat: number; lng: number },
  places: Place[],
  detour: number | null,
  census: { total: number; missing: Record<string, number>; unknown: Record<string, number> } | null,
  speed: number,
  delayPerKmS: number | null,
): FacilityRead[] {
  places = places.filter(freshEligible)
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
            minutes: walkMinutes(best, detour, speed, delayPerKmS),
            inside: nearest.in_circle,
          }
        : null,
      cellsUnknown: census?.unknown[category] ?? 0,
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
  delayPerKmS,
  extentKm,
  onTrip,
}: {
  reads: FacilityRead[]
  detour: number | null
  census: { total: number; blind: number } | null
  delayPerKmS: number | null
  extentKm: string | null
  onTrip?: (category: string) => void
}) {
  return (
    <>
      <details className="exam-sheet-basis"><summary>读数说明：估算时间与网格判定</summary><p className="exam-lead">
        每一类都写出最近的一处，以及按
        {detour ? `本圈实测平均绕行 ${detour.toFixed(2)} 倍` : '直线距离'}
        {delayPerKmS ? `、每公里过街等待约 ${Math.round(delayPerKmS)} 秒` : ''}
        估算的步行时间（参考读数）。
        {census
          ? extentKm
            ? `菜场、药店、小学的方格与地图是同一套：周围 ${extentKm} 公里共 ${census.total} 格，按网格判定，未知格不计入缺失；其中 ${census.blind} 格步行 1 公里内缺至少一类。`
            : `菜场、药店、小学的方格与地图是同一套：15 分钟圈内共 ${census.total} 格，按网格判定，未知格不计入缺失；其中 ${census.blind} 格步行 1 公里内缺至少一类。`
          : ''}
      </p></details>
      <ul className="exam-list facility-sheet">
        {reads.map((item) => (
          <li key={item.category}>
            <div className="exam-cat">
              <span className="exam-facility-icon" style={{ background: PLACE_MARK[item.category]?.color }}>{PLACE_MARK[item.category]?.glyph}</span><b>{item.category}</b>
              {item.nearest ? (
                <span className={item.nearest.inside ? 'exam-min in' : 'exam-min out'}>
                  {item.nearest.minutes}
                  <em>分钟 · 估算</em>
                </span>
              ) : (
                <span className="exam-min out">
                  —<em>没有</em>
                </span>
              )}
            </div>
            <strong className="exam-facility-name">{item.nearest?.name ?? '检索范围内没有这一类'}</strong>
            {item.nearest && <div className="exam-facility-meta"><span>直线 {Math.round(item.nearest.straightM)} 米</span><span>{item.nearest.inside ? '生活圈内' : '生活圈外'}</span><span>圈内 {item.inCircle} 处</span></div>}
            {item.cellsMissing != null && census && <div className="exam-cell-status">
              <p>{census.total > item.cellsUnknown ? `步行 1 公里内缺失 ${item.cellsMissing} / ${census.total - item.cellsUnknown} 个已判定网格` : '本类网格尚未完成判定'}</p>
              {item.cellsUnknown > 0 && <p className="exam-cell-pending">另 {item.cellsUnknown} 格待确认或测距未完成</p>}
            </div>}
            {onTrip && <div className="exam-facility-action"><button type="button" className="trip-report-link" onClick={() => onTrip(item.category)} aria-label={`${item.category}：查看设施步行路线`}>
              <svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="4" cy="15" r="2" /><circle cx="16" cy="5" r="2" /><path d="M4 13V7a3 3 0 0 1 3-3h2a3 3 0 0 1 0 6h4a3 3 0 0 0 3-3" /></svg><span>怎么走</span><span aria-hidden="true">→</span>
            </button></div>}
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
  onTrip,
  report,
  coverage,
  meta,
  simulation,
  onSimulate,
  onClearSimulation,
  onVerifySite,
  sitePlan,
  sitePlanBusy,
  crosscheck,
  crosscheckBusy,
  onCrosscheck,
  onSimulateSuspect,
  onShareSuspect,
}: Props) {
  const nearbyCount = sumCounts(coverage?.nearby_categories)
  const places = coverage?.places ?? []
  const center = meta?.center
  const blindspots = meta?.blindspots
  const census = gridCensus(blindspots)
  const delay = meta?.delay
  const delayPerKmS = delay?.applied ? delay.delay_per_km_s : null
  const speed = delay?.base_speed_m_per_s ?? meta?.speed_m_per_s ?? 1.17
  const extentKm =
    blindspots?.layout === 'disc' ? ((blindspots.extent_m ?? 1500) / 1000).toFixed(1) : null
  const reads =
    center && places.length > 0
      ? facilityReads(center, places, meta?.mean_detour ?? null, census, speed, delayPerKmS)
      : null
  const quality = meta?.quality
  const grayRegions = report.gray_regions?.regions ?? []

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

      <section className="card">
        <h2 className="card-title">
          体检评分 {report.total.toFixed(0)}（{report.grade}）
        </h2>
        <ScoreRadar report={report} />
        <p className="hint">
          路网三项以<b>理想方格路网</b>为满分：同样 {meta?.minutes ?? 15} 分钟走得到
          {report.grid_area_km2 != null ? ` ${report.grid_area_km2.toFixed(2)} km² ` : ''}
          的菱形、最短与最长方向半径之比 0.71、平均绕行 1.27 倍，红绿灯等待照常扣分。直线画圆
          （{report.ideal_area_km2.toFixed(2)} km²）是任何路网都到不了的上界，只用来说明直线法高估了几倍。
          没测的维度画在圆心、标「待测」，不按 0 分计；总分按实际参与的维度权重归一。
        </p>
      </section>

      {grayRegions.length > 0 && (
        <section className="card">
          <h2 className="card-title">灰色区域（设施匮乏，自动标注）</h2>
          <p className="hint">
            相邻的盲区方格合并成片，按面积编号。成因按直线 1 公里内有没有同类设施区分：
            有却走不到是<b>路网阻隔</b>（该打通），没有是<b>供给缺口</b>（该补设）。
          </p>
          <ol className="region-list">
            {grayRegions
              .filter((r) => r.id)
              .map((r) => (
                <li key={r.id}>
                  <div className="region-head">
                    <b>{r.label}</b>
                    <span>
                      {r.cells} 格 · 约 {r.area_km2} km²
                      {r.in_circle_cells < r.cells ? ` · 圈内 ${r.in_circle_cells} 格` : ''}
                    </span>
                  </div>
                  <ul className="region-causes">
                    {r.diagnosis.map((d) => (
                      <li key={d.category} className={`cause-${d.cause}`}>
                        <span className="region-cat">{d.category}</span>
                        <span className="region-split">
                          {d.cause === 'unknown' ? (
                            '成因未知'
                          ) : (
                            <>
                              {d.supply_cells > 0 && <em className="supply">供给缺口 {d.supply_cells}</em>}
                              {d.barrier_cells > 0 && (
                                <em className="barrier">路网阻隔 {d.barrier_cells}</em>
                              )}
                            </>
                          )}
                        </span>
                        {d.barrier_cells > 0 && d.nearby && (
                          <p>
                            往{d.nearby.direction}直线 {d.nearby.straight_m} 米就有「{d.nearby.place}」
                            {d.nearby.walk_m ? `，步行却要 ${Math.round(d.nearby.walk_m)} 米` : ''}。
                          </p>
                        )}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
          </ol>
          {grayRegions.some((r) => !r.id) && (
            <p className="hint">另有 {grayRegions.filter((r) => !r.id).length} 处零散盲区，地图上同样标灰。</p>
          )}
          {onCrosscheck && onSimulateSuspect && (
            <CrosscheckPanel
              result={crosscheck ?? null}
              busy={Boolean(crosscheckBusy)}
              onRun={onCrosscheck}
              onSimulate={onSimulateSuspect}
              onShare={onShareSuspect}
            />
          )}
        </section>
      )}

      {report.prescriptions && report.prescriptions.length > 0 && (
        <section className="card">
          <h2 className="card-title">自动诊疗</h2>
          <p className="hint">
            {report.prescriptions.some((p) => p.basis)
              ? '按灰色区域的逐格成因开方，不另耗配额：路网阻隔 → 打通，供给缺口 → 补设（最大覆盖贪心选址）。' +
                '覆盖数按本圈典型绕行估算，补设点可「核验选址」用真实路网复核。'
              : '由盲区网格与圈内外设施对照生成，不另耗配额。圈外有、圈内无则优先打通路网。'}
          </p>
          <ol className="plan-list">
            {report.prescriptions.map((p) => {
              const covers = coversText(p)
              return (
                <li
                  key={`${p.action}-${p.category}-${p.lat}-${p.lng}`}
                  className={`action-${p.action}`}
                >
                  <div className="plan-head">
                    <span className={`plan-tag action-${p.action}`}>{ACTION_LABEL[p.action]}</span>
                    {p.category && <span>{p.category}</span>}
                    {covers && <span className="plan-covers">{covers}</span>}
                  </div>
                  <strong>{p.title}</strong>
                  <p>{p.reason}</p>
                  {p.action === 'connect' && p.target && (
                    <p className="plan-target">
                      目标设施「{p.target.name}」· 在这片格子的{p.direction}侧
                      {p.cells ? ` · 被挡在外面的有 ${p.cells} 格` : ''}
                    </p>
                  )}
                  <div className="plan-actions">
                    {p.action === 'site' && p.basis && p.category && onVerifySite && (
                      <button
                        type="button"
                        className="sim-btn primary"
                        disabled={sitePlanBusy != null}
                        onClick={() => onVerifySite(p.category as string)}
                      >
                        {sitePlanBusy === p.category ? '核验中…' : `核验选址「${p.category}」`}
                      </button>
                    )}
                    {/* 打通是修路不是建设施，模拟新建对它没有意义 */}
                    {onSimulate &&
                      p.action !== 'connect' &&
                      p.lat != null &&
                      p.lng != null &&
                      p.category && (
                        <button type="button" className="sim-btn" onClick={() => onSimulate(p)}>
                          评估增设「{p.category}」
                        </button>
                      )}
                  </div>
                </li>
              )
            })}
          </ol>

          {sitePlan && (
            <div className="site-plan">
              <div className="sim-head">
                <span className="sim-tag">核验选址 · {sitePlan.category}</span>
                <span className="hint">
                  缺口 {sitePlan.basis.demand_cells} 格 · 典型绕行 {sitePlan.basis.detour} 倍
                </span>
              </div>
              <ol className="site-candidates">
                {sitePlan.candidates.map((c) => {
                  const best = c.lat === sitePlan.best.lat && c.lng === sitePlan.best.lng
                  return (
                    <li key={`${c.lat}-${c.lng}`} className={best ? 'best' : ''}>
                      <span>
                        备选 {c.rank_estimate}
                        {c.region ? ` · 灰色区域 ${c.region}` : ''}
                        {best ? ' · 推荐' : ''}
                      </span>
                      <span>
                        估算 {c.estimated} 格 →{' '}
                        {c.verified != null ? `实测消去 ${c.verified} 格` : STATUS_LABEL[c.status]}
                      </span>
                    </li>
                  )
                })}
              </ol>
              {sitePlan.best.place?.address && (
                <p className="plan-target">
                  推荐点位：{sitePlan.best.place.address}
                  {sitePlan.best.place.description ? `（${sitePlan.best.place.description}）` : ''}
                </p>
              )}
              <p className="hint">
                {sitePlan.note}
                {sitePlan.quota
                  ? ` 本次消耗 ${sitePlan.quota.matrix_pairs} 个点对、${sitePlan.quota.regeo_queries} 次逆地理编码。`
                  : ''}
                推荐点的效果已画在地图上。
              </p>
            </div>
          )}

          {simulation && (
            <div className="sim-result">
              <div className="sim-head">
                <span className="sim-tag">假设效果 · {simulation.category}</span>
                {onClearSimulation && (
                  <button type="button" className="sim-clear" onClick={onClearSimulation}>
                    清除模拟
                  </button>
                )}
              </div>
              <ul className="sim-metrics">
                {simulation.grid_evaluated !== false && <li>
                  <span>改善该品类网格</span>
                  <b>{simulation.covered_count} 个</b>
                </li>}
                {simulation.grid_evaluated !== false && <li>
                  <span>盲区网格总数</span>
                  <b>
                    {simulation.before.blind_count ?? '—'} → {simulation.after.blind_count ?? '—'}
                  </b>
                </li>}
                {simulation.facility_count_before != null && simulation.facility_count_after != null && <li>
                  <span>圈内该类设施</span>
                  <b>{simulation.facility_count_before} → {simulation.facility_count_after} 处</b>
                </li>}
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
              <p className="hint">
                {simulation.grid_evaluated === false || simulation.candidate_count === 0 ? '' : simulation.basis === 'network'
                  ? `候选 ${simulation.candidate_count} 格，实测步行够得着 ${simulation.covered_count} 格。`
                  : '直线估算，是覆盖的上限。'}
                {simulation.approximation}
              </p>
            </div>
          )}
        </section>
      )}

      {(meta?.warnings?.length ?? 0) > 0 && (
        <section className="card warn-card" role="status" aria-label="本次分析的降级说明">
          <h2 className="card-title">这次分析有 {meta?.warnings?.length} 处降级</h2>
          <ul className="warn-list">
            {meta?.warnings?.map((w, i) => (
              <li key={`${w.stage}-${i}`}>
                {w.stage_label && <b>{w.stage_label}：</b>}
                {w.message}
              </li>
            ))}
          </ul>
          <p className="hint">
            降级是为了不把「没测到」写成「没有」：未能判定的部分不计入评分，也不会被画成盲区。
          </p>
        </section>
      )}

      {meta?.traffic && (
        <section className="card">
          <h2 className="card-title">实时路况的时效</h2>
          <p>
            本次用到的路况里，最旧的是 <b>{meta.traffic.max_age_min}</b> 分钟前查到的
            （{meta.traffic.fresh_ttl_min} 分钟内的缓存视为实时）。
            {meta.traffic.stale_points > 0
              ? ` 其中 ${meta.traffic.stale_points} 个采样点因接口失败用了旧路况兜底，最旧 ${meta.traffic.stale_max_age_min ?? '—'} 分钟前。`
              : ''}
          </p>
        </section>
      )}

      {(delay || meta?.refine_skipped) && (
        <section className="card">
          <h2 className="card-title">过街等待与施工围挡</h2>
          {delay?.applied ? (
            <>
              <ul className="sim-metrics">
                <li>
                  <span>取到步行路线的方向</span>
                  <b>
                    {delay.routes_ok} / {delay.routes_requested}
                  </b>
                </li>
                <li>
                  <span>每公里多出的等待</span>
                  <b>{delay.delay_per_km_s != null ? `${Math.round(delay.delay_per_km_s)} 秒` : '—'}</b>
                </li>
                <li>
                  <span>边界以内的过街次数（各方向合计）</span>
                  <b>{delay.boundary_crossings} 次</b>
                </li>
                <li>
                  <span>等时圈面积（不计等待 → 计入等待）</span>
                  <b>
                    {delay.raw_area_km2.toFixed(2)} → {delay.area_km2.toFixed(2)} km²
                  </b>
                </li>
                {delay.closures.length > 0 && (
                  <li>
                    <span>施工围挡 · 被截断的方向</span>
                    <b>
                      {delay.closures.length} 处 · {quality?.closure_truncated ?? delay.closure_rays} 个
                    </b>
                  </li>
                )}
                {(quality?.route_failed ?? 0) > 0 && (
                  <li>
                    <span>路线没取到、按不含等待计算的方向</span>
                    <b>{quality?.route_failed} 个</b>
                  </li>
                )}
              </ul>
              <p className="hint">
                地图上的虚线是不计过街等待时的圈。{delay.note}
              </p>
            </>
          ) : delay ? (
            <p className="hint">
              本次未补过街等待
              {delay.closures.length > 0
                ? `，只按 ${delay.closures.length} 处施工围挡截断受阻方向。`
                : '。'}
              {delay.note}
            </p>
          ) : (
            <p className="hint">{meta?.refine_skipped}</p>
          )}
          {blindspots?.closure_check && (
            <p className="hint">
              围挡核验：取路线 {blindspots.closure_check.checked_pairs} 条，其中{' '}
              {blindspots.closure_check.blocked_pairs} 条被挡住、改找下一家；
              {blindspots.closure_check.unverified_pairs} 条无法核验，记为未知；
              围挡内的设施 {blindspots.closure_check.excluded_places} 处视为暂不可用。
            </p>
          )}
        </section>
      )}

      <section className="card">
        <h2 className="card-title">圈内各类民生设施覆盖</h2>
        {report.categories ? (
          <>
            <p className="exam-lead">
              {meta?.minutes ?? 15} 分钟步行圈内的设施数（深色）与检索半径内的总数（浅色）。
              浅色比深色长的部分，就是「附近有、走不进圈」的设施。
            </p>
            {coverage?.fresh_breakdown && <p className="hint">买菜候选：已确认 {coverage.fresh_breakdown.verified} 家，规则推定 {coverage.fresh_breakdown.inferred} 家，是否卖菜待确认 {coverage.fresh_breakdown.pending} 家。覆盖数量只含前两类，规则推定未逐店核实。</p>}
            {coverage?.fresh_needs_refresh && <p className="hint">旧快照的买菜网格尚未按新规则重测，已标为待确认；重新体检可更新判定。</p>}
            <ul className="cover-bars" aria-label="圈内各类设施数量">
              {coverEntries.map(([name, count]) => {
                const nearby = Math.max(count, coverage?.nearby_categories?.[name] ?? count)
                return (
                  <li key={name} className={count === 0 && !report.uncertain_categories?.includes(name) ? 'blind' : ''}>
                    <span className="cover-name">{name}</span>
                    <span className="cover-track">
                      <i className="cover-out" style={{ width: `${(nearby / maxCover) * 100}%` }} />
                      <i className="cover-in" style={{ width: `${(count / maxCover) * 100}%` }} />
                    </span>
                    <span className="cover-num">
                      {report.uncertain_categories?.includes(name) ? '待确认' : count === 0 ? '缺失' : count}
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
        ) : (
          <p className="hint">本次没有做设施采集，覆盖统计待测。</p>
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
            <h3>网格盲区（步行 1 公里到不了的方格占比）</h3>
            <p>
              {report.blind_cell_count} / {report.cell_count} 个方格步行 1 公里内
              至少缺一类关键设施
              {report.cells_in_circle != null
                ? `，其中 15 分钟圈内 ${report.blind_in_circle} / ${report.cells_in_circle} 格`
                : ''}
              。
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
              占比为该品类步行 1 公里不可达的网格比例，测距失败或买菜能力待确认的网格不计入。
            </p>
          </div>
        )}
      </section>

      {reads && (
        <section className="card">
          <h2 className="card-title">逐类最近设施</h2>
          <FacilitySheet
            reads={reads}
            detour={meta?.mean_detour ?? null}
            census={census}
            delayPerKmS={delayPerKmS}
            extentKm={extentKm}
            onTrip={onTrip}
          />
        </section>
      )}
    </>
  )
}
