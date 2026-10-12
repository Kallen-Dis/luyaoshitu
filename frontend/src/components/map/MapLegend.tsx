import { HEAT_MAX, heatGradientCss } from '../../lib/heatRaster'
import { blindArea, blindCells, cellCount, gridCensus } from '../../lib/grid'
import type { ResultView } from '../../lib/resultView'
import type { IsochroneFeature, SimulationResult } from '../../types'
import { BLIND_COLORS, PLACE_MARK, heatTargetS, type LayerState } from './context'

interface Props {
  tripShown?: boolean
  isochrone: IsochroneFeature | null
  layers: LayerState
  blindCategory: string
  simulation?: SimulationResult | null
  closures: number
  suspects: number
  recheckRays: number
  sites: number
  markings: number
  hasAltRing: boolean
  resultView: ResultView
  /** 3D / 卫星视角：热力按方格画，不做格间平滑 */
  vectorHeat?: boolean
}

/** 图例只列地图上此刻画着的东西：关掉的层、没有数据的层都不出现。 */
export function MapLegend({
  tripShown = false,
  isochrone,
  layers,
  blindCategory,
  simulation,
  closures,
  suspects,
  recheckRays,
  sites,
  markings,
  hasAltRing,
  resultView,
  vectorHeat = false,
}: Props) {
  const p = isochrone?.properties
  const blindspots = p?.blindspots
  const rawShown = Boolean(p?.delay?.applied && p?.raw_ring)
  const inner = layers.innerRings ? (p?.rings ?? []) : []
  const shownBlind = blindspots ? blindArea(blindCells(blindspots, blindCategory, simulation)) : 0
  // 分母按面积：细分出的小格中心在圈外的不计，可能比方格数略少（曹杨 144 格、按面积 141 格）
  const blindTotal = gridCensus(blindspots)?.units ?? blindspots?.cell_count ?? 0
  const refined = Boolean(blindspots?.refinement?.split_cells)
  const labeled = (p?.report?.gray_regions?.regions ?? []).filter((r) => r.id).length
  const blindColor = BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all
  const places = (p?.coverage?.places ?? []).filter(place => place.fresh_status !== 'excluded' && (layers.outsidePlaces || place.in_circle))
  const present = new Set(places.map(place => place.category))
  const userPlaces = places.some((place) => place.source === 'user')
  // 色阶刻度：0 到阈值的 4/3 倍，等分四段（15 分钟阈值即 0 / 5 / 10 / 15 / 20 分钟）
  const targetMin = heatTargetS(p?.minutes) / 60
  const ticks = [0, 1, 2, 3, 4].map((k) => Math.round((targetMin * HEAT_MAX * k) / 4))
  const unmeasured = (blindspots?.cells ?? []).filter((c) => c.reach_s === null && !c.closure_blocked).length
  const blocked = (blindspots?.cells ?? []).filter((c) => c.closure_blocked).length
  const check = p?.gridcheck ?? null

  return (
    <div className="map-legend floating" aria-label="图例">
      {tripShown && <div className="legend-row"><span className="legend-swatch ring-outer" /><span>出行路线：实线为百度路线，品类色虚线为路线缺失 / 估算，红色虚线为围挡受阻，灰色短虚线为端点连接（未核验）。</span></div>}
      <div className="legend-row">
        <span className="legend-swatch ring-outer" />
        <span>
          {p?.minutes ?? 15} 分钟步行圈{rawShown ? '（含过街等待）' : ''}
        </span>
      </div>
      {rawShown && (
        <div className="legend-row">
          <span className="legend-swatch ring-raw" />
          <span>虚线：不计过街等待时的圈</span>
        </div>
      )}
      {inner.length > 0 && (
        <div className="legend-row">
          <span className="legend-swatch ring-inner" />
          <span>{inner.map((r) => r.minutes).join(' / ')} 分钟内圈</span>
        </div>
      )}
      {hasAltRing && (
        <div className="legend-row">
          <span className="legend-swatch mk-baseline" />
          <span>
            {resultView === 'algorithm' ? '点线：叠加标注后的圈' : '点线：不含共享围挡的纯算法圈'}
          </span>
        </div>
      )}

      {layers.heat && blindspots && (
        <>
          <div className="legend-row legend-heat">
            <span className="legend-title">从中心步行</span>
            <span className="legend-heat-scale">
              <i style={{ background: heatGradientCss() }} />
              <span className="legend-heat-ticks">
                {ticks.map((t, k) => (
                  <em key={k} style={{ left: `${k * 25}%` }}>
                    {k === ticks.length - 1 ? `${t}+` : t}
                  </em>
                ))}
              </span>
            </span>
            <span className="legend-ends">分钟</span>
          </div>
          {unmeasured > 0 && (
            <div className="legend-row">
              <span className="legend-swatch unknown" />
              <span>浅蓝方格 {unmeasured} 个：没测到从中心过去的耗时（接口没返回结果），不是走不到</span>
            </div>
          )}
          {blocked > 0 && (
            <div className="legend-row">
              <span className="legend-swatch heat-blocked" />
              <span>褐色方格 {blocked} 个：从中心过来的路被施工围挡挡住，不画耗时</span>
            </div>
          )}
          {vectorHeat && (
            <div className="legend-row">
              <span>3D / 卫星视角下按方格着色，不做格间平滑</span>
            </div>
          )}
        </>
      )}


      {layers.blind && blindspots && (
        <div className="legend-row">
          <span
            className="legend-swatch blind"
            style={{ background: `${blindColor}44`, borderColor: blindColor }}
          />
          <span>
            {blindCategory === 'all' ? '缺任一类设施' : `缺${blindCategory}`}：{cellCount(shownBlind)} /{' '}
            {cellCount(blindTotal)} 格
            {blindspots.layout === 'disc' ? '' : '（只看 15 分钟圈内）'}
            {refined ? '；边界格切成 50 米小格，按面积折算' : ''}
          </span>
        </div>
      )}
      {layers.regions && blindCategory === 'all' && labeled > 0 && (
        <div className="legend-row">
          <span className="legend-swatch region-outline" />
          <span>
            {simulation ? '模拟新建时不画区域外框（区域按新建前划定）' : `灰色区域 ${labeled} 片，点编号看成因`}
          </span>
        </div>
      )}
      {check && !simulation && layers.checkGaps && check.gaps.length > 0 && (
        <div className="legend-row">
          <span className="legend-swatch check-gap" />
          <span>斜线：射线圈里确认走不到的缺口 {check.gaps.length} 片（网格交叉校核，点开看证据）</span>
        </div>
      )}
      {check && !simulation && layers.checkEstimate && (
        <div className="legend-row">
          <span className="legend-swatch check-estimate" />
          <span>点划线：把射线点和网格点放在一起重新估的 15 分钟边界，{check.estimate_area_km2.toFixed(2)} km²，只作对照，评分仍按实线圈</span>
        </div>
      )}
      {check && !simulation && layers.detourClues && check.clues.length > 0 && (
        <div className="legend-row">
          <span className="legend-swatch check-clue" />
          <span>赭色线：绕行线索。线两边的方格挨着，从中心走过去却要多绕几百米，中间可能隔着围墙、河道等；线只标大致位置</span>
        </div>
      )}

      {present.size > 0 && (
        <div className="legend-marks compact">
          {Object.entries(PLACE_MARK)
            .filter(([name]) => present.has(name))
            .map(([name, mark]) => (
              <span key={name} className="legend-mark">
                <i style={{ background: mark.color }}>{mark.glyph}</i>
                {name}
              </span>
            ))}
          {userPlaces && (
            <span className="legend-mark">
              <i className="user">补</i>
              用户补录
            </span>
          )}
        </div>
      )}
      {/* 和地图上的标记同样画法：浅底、虚线圈的「菜」 */}
      {places.some((place) => place.fresh_status === 'pending') && (
        <div className="legend-row">
          <span className="legend-mark compact-mark">
            <i className="fresh-pending">菜</i>
          </span>
          <span>超市，但不确定是否卖菜</span>
        </div>
      )}

      {closures > 0 && (
        <div className="legend-row">
          <span className="legend-swatch closure" />
          <span>假设围挡 {closures} 处（仅规划模拟）</span>
        </div>
      )}
      {layers.clues && suspects > 0 && (
        <div className="legend-row">
          <span className="legend-swatch suspect" />
          <span>复测疑似阻断 {suspects} 处，点开确认</span>
        </div>
      )}
      {layers.clues && recheckRays > 0 && (
        <div className="legend-row">
          <span className="legend-swatch ring-raw" />
          <span>复测：灰虚线旧路线、彩线新路线（绿色为变短）</span>
        </div>
      )}
      {layers.clues && sites > 0 && (
        <div className="legend-row">
          <span className="legend-mark">
            <i style={{ background: '#b45309', borderRadius: '4px' }}>工</i>
          </span>
          <span>工地候选 {sites} 处，点开确认</span>
        </div>
      )}
      {layers.markings && markings > 0 && (
        <div className="legend-row">
          <span className="legend-swatch mk-shared" />
          <span>共享标注 {markings} 条：实底已核实、虚框待核实</span>
        </div>
      )}
      {simulation && (
        <div className="legend-row">
          <span className="legend-swatch plan" />
          <span>
            拟建点 ·{' '}
            {simulation.grid_evaluated === false || simulation.candidate_count === 0 ? '设施覆盖与评分对比' : simulation.basis === 'network'
              ? '步行 1 公里内够得着的方格已消去'
              : '直线估算（上限），虚线圈内方格已消去'}
          </span>
        </div>
      )}
    </div>
  )
}
