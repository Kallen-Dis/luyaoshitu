import { freshEligible } from '../lib/fresh'
import { HoleEvidence } from './HoleEvidence'
import { cellCount, gridCensus } from '../lib/grid'
import { areaDelta } from '../lib/gridCheck'
import { CrosscheckPanel } from './CrosscheckPanel'
import { ScoreRadar } from './ScoreRadar'
import { ReportSection } from './ReportSection'
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
  onCompareMethods?: () => void
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
  census: { total: number; units: number; blind: number; refined?: boolean } | null
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
            : `菜场、药店、小学的方格与地图是同一套：15 分钟圈内共 ${census.total} 格，按网格判定，未知格不计入缺失；其中 ${cellCount(census.blind)} 格步行 1 公里内缺至少一类${census.refined ? '（与邻格结论不同的边界格切成 50 米小格实测，按面积折算，子格不和父格重复计）' : ''}。`
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
              <p>{census.units > item.cellsUnknown ? `步行 1 公里内缺失 ${cellCount(item.cellsMissing)} / ${cellCount(census.units - item.cellsUnknown)} 个已判定网格` : '本类网格尚未完成判定'}</p>
              {item.cellsUnknown > 0 && <p className="exam-cell-pending">另 {cellCount(item.cellsUnknown)} 格待确认或测距未完成</p>}
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
  onCompareMethods,
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
  const check = meta?.gridcheck ?? null
  const fusion = meta?.fusion
  const validationCoverage = (fusion?.validation ?? meta?.fusion_review?.validation)?.coverage
  const validationPlanNote = validationCoverage?.planned_points != null && <p className="hint">
    本轮已安排 {validationCoverage.planned_points} 个核验目标，需覆盖 {validationCoverage.required_regions} 个修正片区。
    {(validationCoverage.unplanned_regions?.length ?? 0) > 0 && `其中 ${validationCoverage.unplanned_regions?.length} 个片区尚未找到独立测点。`}
    {(validationCoverage.budget?.shortfall ?? 0) > 0 && `按当前预算，${validationCoverage.budget?.planned_requests != null ? '覆盖主目标、冻结备选及有限重试' : '完成主目标计划'}还缺 ${validationCoverage.budget?.shortfall} 次路线请求；所有请求仍受原上限约束。`}
  </p>
  const checkSpacing = blindspots?.grid_spacing_m ?? 100
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
          体检评分 {report.total.toFixed(1)}（{report.grade}）
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
        <ReportSection title="灰色区域（设施匮乏，自动标注）" defaultExpanded={false}>
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
        </ReportSection>
      )}

      {report.prescriptions && report.prescriptions.length > 0 && (
        <ReportSection title="自动诊疗">
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
        </ReportSection>
      )}

      {(meta?.warnings?.length ?? 0) > 0 && (
        <ReportSection title={`这次分析有 ${meta?.warnings?.length} 处降级`} className="warn-card" role="status" ariaLabel="本次分析的降级说明">
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
        </ReportSection>
      )}

      {meta?.traffic && (
        <ReportSection title="实时路况的时效">
          <p>
            本次用到的路况里，最旧的是 <b>{meta.traffic.max_age_min}</b> 分钟前查到的
            （{meta.traffic.fresh_ttl_min} 分钟内的缓存视为实时）。
            {meta.traffic.stale_points > 0
              ? ` 其中 ${meta.traffic.stale_points} 个采样点因接口失败用了旧路况兜底，最旧 ${meta.traffic.stale_max_age_min ?? '—'} 分钟前。`
              : ''}
          </p>
        </ReportSection>
      )}

      {(delay || meta?.refine_skipped) && (
        <ReportSection title="过街等待与施工围挡">
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
                  <span>{fusion ? '原射线参考圈' : '等时圈面积'}（不计等待 → 计入等待）</span>
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
                {fusion ? '这是原射线圈的过街校正对照；当前地图主圈采用融合估计。' : '地图上的虚线是不计过街等待时的圈。'}{delay.note}
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
        </ReportSection>
      )}

      {check && fusion && (
        <ReportSection title="生活圈校正与方法对照" defaultExpanded={false}>
          {fusion.quality?.validation_status === 'estimated' && <p className="hint">当前采用融合估计：基础观测、路径与几何检查已通过，独立抽样尚未完成。这份范围是模型估计，不能视为完整核验或实地测量结果。</p>}
          {!!fusion.quality?.local_hole_estimate_count && <p className="hint">当前保留 {fusion.quality.local_hole_estimate_count} 处小范围局部排除估计，内部可靠超时点未填绿；这些小孔洞的边界尚待独立核验，不单独阻断整份融合估计。</p>}
          {!!fusion.quality?.water_path_conflicts && <p className="hint">部分路线段与已知水域相交，可能涉及未收录桥面或岸线偏差。这些段不作为路径约束，已知水面仍排除；当前范围保留为融合估计，跨水证据尚待核验。</p>}
          {fusion.quality?.geographic_verification_pending && <p className="hint">水域数据服务暂不可用，当前采用路网融合估计，水域边界尚未核验；服务故障不表示此处存在水域冲突。</p>}
          {fusion.quality?.validation_status === 'verified' && <p className="hint">当前采用融合范围，已通过本轮独立抽样核验；原射线范围保留作方法对照。</p>}
          {validationPlanNote}
          {onCompareMethods && <button type="button" className="btn small" onClick={onCompareMethods}>查看修正前后</button>}
          <ul className="sim-metrics">
            <li><span>融合范围（当前面积与评分口径）</span><b>{fusion.area_km2.toFixed(3)} km²</b></li>
            <li><span>原射线范围（对照）</span><b>{fusion.ray_area_km2.toFixed(3)} km²</b></li>
            {fusion.ray_statistics?.score != null && <li><span>原射线口径参考分</span><b>{fusion.ray_statistics.score.toFixed(1)}</b></li>}
            {fusion.ray_statistics?.blind_count != null && <li><span>原圈设施盲区（参考，未消失）</span><b>{cellCount(fusion.ray_statistics.blind_count)} 格</b></li>}
            <li><span>补回原圈外的估计范围</span><b>{Math.round(fusion.added_area_km2 * 1e6).toLocaleString()} m²</b></li>
            <li><span>原圈内暂不纳入的范围</span><b>{Math.round(fusion.omitted_ray_area_km2 * 1e6).toLocaleString()} m²</b></li>
            <li><span>{fusion.unknown_inside_ray_km2 > 0 ? '其中待核实（不能判为超时）' : '单独测量缺失记录'}</span><b>{fusion.unknown_inside_ray_km2 > 0 ? `${Math.round(fusion.unknown_inside_ray_km2 * 1e6).toLocaleString()} m²` : `${fusion.quality?.missing_observations ?? fusion.pending_points.length} 个点`}</b></li>
            <li><span>原圈内红色超时采样证据</span><b>{check.gaps.length} 片</b></li>
            <li><span>绕行线索</span><b>{check.clue_summary.shown} 段</b></li>
            {fusion.validation && <li><span>留出点核验 · 判定分歧</span><b>{fusion.validation.checked} · {fusion.validation.disagreements ?? fusion.validation.corrections}</b></li>}
            {fusion.validation?.coverage && <li><span>修正片区核验覆盖</span><b>{fusion.validation.coverage.checked_regions} / {fusion.validation.coverage.required_regions}</b></li>}
            {fusion.completion && <li><span>单点路线位置核验 · 实际终点观测</span><b>{fusion.completion.route_verified_points} · {fusion.completion.relocated_observations}</b></li>}
            {meta?.fusion_budget && <li><span>融合与统计点对 · 路线请求（含重试）</span><b>{meta.fusion_budget.pair_attempts ?? meta.fusion_budget.matrix_pairs} / {meta.fusion_budget.max_pairs} · {meta.fusion_budget.route_attempts ?? meta.fusion_budget.routes} / {meta.fusion_budget.max_routes}</b></li>}
          </ul>
          <p className="hint">射线提供基础耗时，网格和可信路线在局部校正，再取 {meta?.minutes ?? 15} 分钟边界。缺少直接观测的位置仍是模型估计，并非逐点实测。主范围、热力、设施数量和盲区统计使用这份融合结果。方向均衡与路径效率仍来自射线及路线采样。</p>
          {!!fusion.quality?.micro_hole_regularization?.filled_count && <p className="hint">模型已填补 {fusion.quality.micro_hole_regularization.filled_count} 处微小插值空洞，共 {fusion.quality.micro_hole_regularization.filled_area_m2.toFixed(1)} 平方米。这属于有界模型整理，不是新增路线实测；已知水面及可靠超时证据仍予保留。</p>}
          {['ray-local-v3.3', 'ray-local-v3.4'].includes(fusion.version) && <p className="hint">校正只在可靠路线观测附近生效，其他位置沿用射线估计。核验覆盖关键修正片区和不同方向；临界耗时不会强制改变圈内外分类。</p>}
          {['ray-path-v3.5', 'ray-path-v3.6', 'ray-path-v3.7'].includes(fusion.version) && <p className="hint">可信路线沿途提供可达上限，超时点只影响附近范围。5、10、15 分钟圈的内部孔洞分别检查路线冲突和证据稳定性；新区域核验与已有路线检查分开进行。</p>}
          {['ray-path-v3.7', 'ray-path-v3.8'].includes(fusion.version) && <p className="hint">模型估计接近时间边界的位置优先补测步行路线，再局部重算；十五分钟判定仍以 900 秒为界。用于校正的测点不重复计作独立验收证据。</p>}
          {fusion.version === 'ray-path-v3.8' && <p className="hint">路线须从同一位置出发，并核对返回终点。单个超时响应不会强制挖出孔洞；修正在独立核验前预审，无法安全采用时保留射线估计。未通过检查的辅助内圈暂不展示。</p>}
          {['ray-path-v3.9', 'ray-path-v3.10', 'ray-path-v3.11'].includes(fusion.version) && <p className="hint">内圈按超时测点、外围可达路线及稳定性逐个核验。绕行线索帮助定位补测与限制插值，不代表实际围墙；有证据且独立验收通过的内圈予以保留。</p>}
          {['ray-path-v3.10', 'ray-path-v3.11'].includes(fusion.version) && <p className="hint">计算网格加密后，孔洞仍需保持数量和边界稳定；未通过时保留射线估计范围。</p>}
          {fusion.quality?.hole_records?.map(hole => <details key={hole.id}><summary>内部排除区 {hole.id + 1} · 查看证据</summary><HoleEvidence hole={hole} accepted={fusion.quality?.hole_validation_complete} localEstimate={!hole.supported && !!fusion.quality?.local_hole_estimate_count} /></details>)}
          {fusion.version === 'ray-grid-v2' && <p className="hint">先复用缓存路线，再核验缺测与近边界点。有效路线采用自身耗时；终点被吸附时，观测放回实际位置。其余测距配过街等待构成模型估计，强绕行处按最近观测点分侧处理。无待核实表示主范围没有缺测空洞，仍是采样估计，不代表每个门口都已实测。</p>}
          <p className="hint">当前分数只评价纳入融合范围的居民点。原圈中被排除或待核实的居民仍可能缺设施，盲区占比变小不能视为居民出行改善。</p>
          <p className="hint">在地图“图层”里点击“查看方法对照”，先查看原射线虚线与当前边界；红色超时证据、补回范围和绕行线索可分别开启。红色表示从中心出发超时；灰色设施盲区判断步行 1 公里内有没有设施，两者是不同问题。</p>
          {fusion.validation && <p className="hint">额外核验已完成 {fusion.validation.checked} 个点，另有 {fusion.validation.unknown} 个未完成。这些点始终不参与拟合；分歧保留在核验记录中。这是地图接口之间的核对，不代表实地准确率。</p>}
          {meta?.fusion_budget && (meta.fusion_budget.route_attempts ?? 0) > meta.fusion_budget.max_routes && <p className="hint">此快照首轮路线查询因重试曾超出请求预算；后续已修复预算闸门，重试也受上限约束。</p>}
          {check.stopped && <p className="hint">{fusion.unknown_inside_ray_km2 > 0 ? `补测未全部完成（${check.stopped}），真正缺测的位置保留待核实。` : '本次仅在已有预算和缓存内完成估计，未完成的额外核验点仍单独记录。'}</p>}
        </ReportSection>
      )}
      {check && !fusion && (
        <ReportSection title="网格交叉校核" defaultExpanded={false}>
          {validationPlanNote}
          {meta?.geographic_warning && <p className="hint">{meta.geographic_warning}</p>}
          {meta?.geographic_domain?.status === 'covered' && <p className="hint">范围已按水域边界约束；来源：{meta.geographic_domain.source}，版本：{meta.geographic_domain.version}。
            {meta.geographic_domain.provider?.startsWith('osm-water') && <> 数据来自 <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap 贡献者</a>；采用本地近似坐标换算，岸线和步行桥面仍可能存在误差。</>}
          </p>}
          {meta?.geographic_domain?.status === 'unavailable' && <p className="hint">{meta.geographic_domain.reason}。水域核验未完成。</p>}
          {meta?.fusion_fallback && <p className="hint">{meta.fusion_fallback}。面积、评分与设施统计继续使用原范围。</p>}
          {!!meta?.fusion_review?.quality?.micro_hole_regularization?.filled_count && <p className="hint">候选模型已填补 {meta.fusion_review.quality.micro_hole_regularization.filled_count} 处微小插值空洞，共 {meta.fusion_review.quality.micro_hole_regularization.filled_area_m2.toFixed(1)} 平方米。整理结果仍需通过其余核验，不能视为新增路线实测。</p>}
          {meta?.fusion_review?.quality?.hole_records?.map(hole => <details key={hole.id}><summary>内圈候选 {hole.id + 1} · 查看审核</summary><HoleEvidence hole={hole} /></details>)}
          {meta?.fusion_review?.version === 'ray-path-v3.8' && <p className="hint">当前主范围采用射线估计。融合候选在核验前检查起终点、修正证据和预算；预审未通过时仅复核已有缓存，不新增验收请求。红色为超时采样及其局部估计，不能视为整片逐点实测。</p>}
          {meta?.fusion_review?.validation && <p className="hint">留出点核验完成 {meta.fusion_review.validation.checked} 个，判定分歧 {meta.fusion_review.validation.disagreements ?? 0} 个，另有 {meta.fusion_review.validation.unknown} 个未完成。核验点没有回填参与拟合。</p>}
          {meta?.fusion_review?.validation?.coverage && <p className="hint">关键修正片区已核验 {meta.fusion_review.validation.coverage.checked_regions} / {meta.fusion_review.validation.coverage.required_regions}，方向覆盖 {meta.fusion_review.validation.coverage.checked_sectors} / {meta.fusion_review.validation.coverage.required_sectors}。覆盖不足时继续使用射线估计。</p>}
          {meta?.fusion_review && check.version === 1 && <p className="hint">下方辅助校核数据沿用原快照，所列路线消耗发生在原快照生成时；它不是本次未通过检查的新范围。</p>}
          <ul className="sim-metrics">
            <li>
              <span>射线法面积（主结果，评分按它）</span>
              <b>{check.ray_area_km2.toFixed(3)} km²</b>
            </li>
            <li>
              <span>{check.version >= 2 ? '融合候选面积（未采用）' : '校核估计面积'}</span>
              <b>
                {check.estimate_area_km2.toFixed(3)} km² · {areaDelta(check)}
              </b>
            </li>
            <li>
              <span>射线圈里确认走不到的缺口</span>
              <b>
                {check.gaps.length} 片 · 约 {Math.round(check.gap_area_km2 * 1e6).toLocaleString()} m²
              </b>
            </li>
            {(check.disputed?.points ?? 0) > 0 && (
              <li>
                <span>存疑、未画出的点（路线与批量算路结论相反）</span>
                <b>
                  {check.disputed?.points} 个 · 约 {(check.disputed?.area_m2 ?? 0).toLocaleString()} m²
                </b>
              </li>
            )}
            <li>
              <span>{check.version >= 2 ? '原圈外、采样估计在时间阈值内的点' : '射线圈外、实测 15 分钟内走得到的点'}</span>
              <b>
                {check.outside_reachable} 个
                {check.disputed?.outside_points ? `（另 ${check.disputed.outside_points} 个路线说超时，存疑不计）` : ''}
              </b>
            </li>
            <li>
              <span>绕行线索（显示 / 候选边）</span>
              <b>
                {check.clue_summary.shown} 段 / {check.clue_summary.edges} 条
              </b>
            </li>
            <li>
              <span>
                测时点：圈内格 · 外圈格 · {checkSpacing / 2} / {checkSpacing / 4} 米子点
              </span>
              <b>
                {check.points.cells} · {check.points.outer} · {check.points.sub50} / {check.points.sub25}
              </b>
            </li>
            {(check.version < 2 || meta?.quota) && <li>
              <span>{check.version >= 2 ? '本次分析实发点对 · 路线请求' : '校核时新增点对 · 步行路线（命中缓存不计）'}</span>
              <b>
                {check.version >= 2 ? meta?.quota?.matrix_pairs : check.usage.matrix_pairs} · {check.version >= 2 ? (meta?.quota?.route_requests ?? 0) : check.usage.routes}
              </b>
            </li>}
          </ul>
          {check.version >= 2 ? <p className="hint">
            当前沿用射线估计。候选与射线接近或重合，表示本次未形成明显修正，不代表核验已经通过。
            {meta?.fusion_fallback ? ` 具体原因：${meta.fusion_fallback}。` : ' 融合候选尚未完成发布核验。'}
            {meta?.fusion_review?.version && ` 本结果计算版本：${meta.fusion_review.version}；历史结果不会自动按新版重算。`}
            面积、评分和设施统计继续按原射线范围计算。留出核验点不参与拟合，未完成核验不等于核验通过。
            红色斜线表示中心出发的超时采样证据，与步行 1 公里到不了设施的灰色盲区不同。
            {check.stopped ? ' 本次额外校核受预算或接口状态限制，尚未全部完成。' : ''}
          </p> : <p className="hint">
            用另一组网格样本交叉核对射线圈：方格的路网距离与不含等待的耗时逐点实测，过街等待仍取自相邻射线，
            只有单独取过路线的 {check.routes.points_verified} 个点用自己的等待，所以不是独立校核。
            缺口只画确认超过 15 分钟的点（不计等待已超时，或用自己的路线补上等待后超时）
            {check.over_unverified > 0 ? `；另有 ${check.over_unverified} 个点只靠插值等待才超时，记为待核实、不画` : ''}。
            取过路线的点，路线自己的耗时也要超过 15 分钟，两者结论相反的记为存疑、不画；
            一个点都没取过路线的片，再取最远的一点核验
            {check.routes.representatives ? `（核验了 ${check.routes.representatives} 片）` : ''}。
            校核估计面积：把射线点和网格点连成三角网，在每个三角形里按位置比例估耗时（不跨绕行线索），再取 15 分钟边界；
            它只作对照，面积和评分仍按射线圈。
            {check.corrected > 0 ? ` 重测纠正了 ${check.corrected} 个格心的缓存值。` : ''}
            {check.stopped ? ` 预算或配额用完，校核没做完（${check.stopped}）。` : ''}
          </p>}
        </ReportSection>
      )}

      <ReportSection title="圈内各类民生设施覆盖">
        {report.categories ? (
          <>
            <p className="exam-lead">
              {meta?.minutes ?? 15} 分钟步行圈内的设施数（深色）与检索半径内的总数（浅色）。
              浅色比深色长的部分，是附近检索到、未计入当前估计圈内的设施；实际能否到达以步行路线为准。
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
      </ReportSection>

      {reads && (
        <ReportSection title="逐类最近设施" defaultExpanded={false}>
          <FacilitySheet
            reads={reads}
            detour={meta?.mean_detour ?? null}
            census={census}
            delayPerKmS={delayPerKmS}
            extentKm={extentKm}
            onTrip={onTrip}
          />
        </ReportSection>
      )}
    </>
  )
}
