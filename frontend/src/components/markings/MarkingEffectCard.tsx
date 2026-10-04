import type { ReactNode } from 'react'
import { TYPE_META } from '../../lib/markings'
import type { MarkingSummary, MarkingsResult } from '../../types'
import { Icon, TypeBadge } from './Icon'
import { StatusStamp } from './MarkingDetail'

interface Props {
  result: MarkingsResult
  /** 用户勾选要采纳的他人待核实标注（下次计算时带上） */
  adopted: number[]
  onToggleAdopt: (id: number) => void
  /** 本次结果之后新出现、尚未计入的附近标注数 */
  pendingNew: number
  mode: 'auto' | 'none'
  onRerun: (mode: 'auto' | 'none') => void
  onSelect: (id: number) => void
  busy: boolean
  /** 结果里带了纯算法的差异时，地图可以即时切换，不必为了「只看纯算法」再算一遍 */
  mapView?: { view: 'markings' | 'algorithm'; onChange: (view: 'markings' | 'algorithm') => void }
}

function signed(value: number | null | undefined, digits = 0, unit = ''): string {
  if (value == null) return '—'
  const fixed = value.toFixed(digits)
  return `${value > 0 ? '+' : ''}${fixed}${unit}`
}

function Row({
  m,
  children,
  onSelect,
}: {
  m: MarkingSummary
  children?: ReactNode
  onSelect: (id: number) => void
}) {
  const meta = TYPE_META[m.type]
  return (
    <li className="mk-effect-row">
      <TypeBadge type={m.type} color={meta.color} soft={meta.soft} size={28} />
      <button type="button" className="mk-effect-body" onClick={() => onSelect(m.id)}>
        <b>{m.title}</b>
        {m.effect && <span>{m.effect}</span>}
        {m.reason && <span className="muted">{m.reason}</span>}
      </button>
      {children}
    </li>
  )
}

/**
 * 报告里的「用户标注的影响」：纯算法结果与叠加标注后的结果并列，逐条写明影响；
 * 他人未核实的标注只作为建议，勾选采纳后重新计算才会计入。
 */
export function MarkingEffectCard({
  result,
  adopted,
  onToggleAdopt,
  pendingNew,
  mode,
  onRerun,
  onSelect,
  busy,
  mapView,
}: Props) {
  const { applied, suggested, skipped, effect, baseline } = result
  const adoptedNow = new Set(adopted)
  // 勾选与这次结果不一致：新勾了建议，或取消了之前采纳的
  const adoptChanged =
    suggested.some((m) => adoptedNow.has(m.id)) ||
    applied.some((m) => !m.mine && m.status !== 'verified' && !adoptedNow.has(m.id))

  return (
    <section className="card mk-effect" aria-label="用户标注的影响">
      <h2 className="card-title">用户标注的影响</h2>

      {result.mode === 'none' ? (
        <p className="hint">这次按纯算法计算，没有叠加附近的 {result.nearby_count} 条标注。</p>
      ) : applied.length === 0 ? (
        <p className="hint">
          附近 {(result.query_radius_m / 1000).toFixed(1)} 公里内有 {result.nearby_count} 条标注，
          这次没有叠加任何一条：他人未核实的标注只作为建议，勾选采纳后重算才会计入。
        </p>
      ) : effect && baseline ? (
        <div className="mk-compare">
          <div>
            <span>纯算法</span>
            <b>{effect.score_algorithm}</b>
            <em>{baseline.grade}</em>
          </div>
          <Icon name="chevron" size={18} />
          <div className="with">
            <span>叠加 {applied.length} 条标注</span>
            <b>{effect.score_with_markings}</b>
            <em className={effect.score_delta && effect.score_delta < 0 ? 'down' : 'up'}>
              {signed(effect.score_delta, 1)}
            </em>
          </div>
          <ul className="mk-compare-stats">
            <li>
              盲区方格 <b>{signed(effect.blind_cells_delta)}</b>
            </li>
            <li>
              等时圈面积 <b>{signed(effect.area_delta_km2, 3, ' km²')}</b>
            </li>
            <li>
              灰色区域 <b>{signed(effect.gray_regions_delta)}</b>
            </li>
          </ul>
        </div>
      ) : (
        <p className="hint">
          叠加了 {applied.length} 条标注，但它们没有改变算法的输入（例如人工灰色区域），分数不变。
        </p>
      )}

      {applied.length > 0 && (
        <>
          <h3 className="mk-subhead">已计入</h3>
          <ul className="mk-effect-list">
            {applied.map((m) => (
              <Row key={m.id} m={m} onSelect={onSelect}>
                {/* 他人未核实、是你采纳进来的：可以在这里取消采纳 */}
                {!m.mine && m.status !== 'verified' ? (
                  <label className="mk-adopt">
                    <input
                      type="checkbox"
                      checked={adoptedNow.has(m.id)}
                      onChange={() => onToggleAdopt(m.id)}
                    />
                    采纳
                  </label>
                ) : (
                  <StatusStamp status={m.status} />
                )}
              </Row>
            ))}
          </ul>
        </>
      )}

      {suggested.length > 0 && (
        <>
          <h3 className="mk-subhead">
            建议 <small>他人提交、尚未核实</small>
          </h3>
          <ul className="mk-effect-list">
            {suggested.map((m) => (
              <Row key={m.id} m={m} onSelect={onSelect}>
                <label className="mk-adopt">
                  <input
                    type="checkbox"
                    checked={adoptedNow.has(m.id)}
                    onChange={() => onToggleAdopt(m.id)}
                  />
                  采纳
                </label>
              </Row>
            ))}
          </ul>
        </>
      )}

      {skipped.length > 0 && (
        <details className="mk-fold">
          <summary>
            未使用 <small>{skipped.length}</small>
          </summary>
          <ul className="mk-effect-list">
            {skipped.map((m) => (
              <Row key={m.id} m={m} onSelect={onSelect} />
            ))}
          </ul>
        </details>
      )}

      <div className="mk-actions">
        {(adoptChanged || pendingNew > 0) && mode === 'auto' && (
          <button type="button" className="mk-btn primary" disabled={busy} onClick={() => onRerun('auto')}>
            {pendingNew > 0 && !adoptChanged ? `计入新的 ${pendingNew} 条并重算` : '按采纳重新计算'}
          </button>
        )}
        {mapView ? (
          <button
            type="button"
            className="mk-btn"
            aria-pressed={mapView.view === 'algorithm'}
            onClick={() => mapView.onChange(mapView.view === 'algorithm' ? 'markings' : 'algorithm')}
          >
            {mapView.view === 'algorithm' ? '地图回到含标注的结果' : '地图上看纯算法结果'}
          </button>
        ) : result.mode !== 'none' ? (
          <button type="button" className="mk-btn" disabled={busy} onClick={() => onRerun('none')}>
            只看纯算法结果
          </button>
        ) : (
          <button type="button" className="mk-btn primary" disabled={busy} onClick={() => onRerun('auto')}>
            叠加标注重新计算
          </button>
        )}
      </div>
      <p className="hint">
        {mapView && '纯算法结果随这份结果一起算好了，地图切换不重算、不花配额。'}
        {(!mapView || ((adoptChanged || pendingNew > 0) && mode === 'auto')) &&
          '重算时已测过的点对直接复用，额外配额只花在新的候选上。'}
      </p>
    </section>
  )
}
