import { HEAT_COLORS } from '../../lib/heatRaster'
import { blindCells } from '../../lib/grid'
import type { ResultView } from '../../lib/resultView'
import type { IsochroneFeature, SimulationResult } from '../../types'
import { BLIND_COLORS, PLACE_MARK, heatScaleS, type LayerState } from './context'

interface Props {
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
  const shownBlind = blindspots ? blindCells(blindspots, blindCategory, simulation).length : 0
  const labeled = (p?.report?.gray_regions?.regions ?? []).filter((r) => r.id).length
  const blindColor = BLIND_COLORS[blindCategory] ?? BLIND_COLORS.all
  const present = new Set(
    (p?.coverage?.places ?? [])
      .filter((place) => layers.outsidePlaces || place.in_circle)
      .map((place) => place.category),
  )
  const userPlaces = (p?.coverage?.places ?? []).some((place) => place.source === 'user')
  const scale = blindspots ? heatScaleS(blindspots.max_reach_s, p?.minutes) : 0

  return (
    <div className="map-legend floating" aria-label="图例">
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
          <div className="legend-row">
            <span className="legend-title">步行耗时</span>
            <span className="legend-scale">
              {HEAT_COLORS.map((c) => (
                <i key={c} style={{ background: c }} />
              ))}
            </span>
            <span className="legend-ends">
              <em>近</em>
              <em>
                {blindspots.max_reach_s !== null
                  ? `${blindspots.max_reach_s > scale ? '≥ ' : ''}${Math.round(scale / 60)} 分钟`
                  : '远'}
              </em>
            </span>
          </div>
          <div className="legend-row">
            <span className="legend-swatch unknown" />
            <span>浅蓝：测距失败；褐色：被围挡挡住。都不代表「很远」</span>
          </div>
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
            {blindCategory === 'all' ? '缺任一类设施' : `缺${blindCategory}`}：{shownBlind} /{' '}
            {blindspots.cell_count} 格
            {blindspots.layout === 'disc' ? '' : '（只看 15 分钟圈内）'}
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

      {closures > 0 && (
        <div className="legend-row">
          <span className="legend-swatch closure" />
          <span>临时围挡 {closures} 处（只算你这次）</span>
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
            {simulation.basis === 'network'
              ? '步行 1 公里内够得着的方格已消去'
              : '直线估算（上限），虚线圈内方格已消去'}
          </span>
        </div>
      )}
    </div>
  )
}
