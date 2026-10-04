import { useEffect, useState } from 'react'
import { fetchMyMarkings } from '../../api'
import { TYPE_META, formatDate, formatDistance } from '../../lib/markings'
import type { Marking, MarkingConfig } from '../../types'
import { Icon, TypeBadge } from './Icon'
import { MarkingDetailView, StatusStamp } from './MarkingDetail'
import type { ToastSpec } from './UndoToast'

interface Props {
  config: MarkingConfig
  nearby: Marking[]
  loading: boolean
  error: string | null
  radiusM: number
  selectedId: number | null
  onSelect: (id: number | null) => void
  onCreate: () => void
  onChanged: (marking: Marking) => void
  onToast: (toast: Omit<ToastSpec, 'id'>) => void
  onLocate: (marking: Marking) => void
  /** 标注有变化时加一，「我的」列表据此重新拉取 */
  revision: number
}

type Tab = 'nearby' | 'mine'

function MarkingRow({
  m,
  selected,
  onClick,
  showDate,
}: {
  m: Marking
  selected: boolean
  onClick: () => void
  showDate?: boolean
}) {
  const meta = TYPE_META[m.type]
  return (
    <li>
      <button
        type="button"
        className={selected ? 'mk-row selected' : 'mk-row'}
        onClick={onClick}
        aria-current={selected || undefined}
      >
        <TypeBadge type={m.type} color={meta.color} soft={meta.soft} />
        <span className="mk-row-body">
          <b>{m.title}</b>
          <span className="mk-row-meta">
            {showDate ? formatDate(m.created_at) : formatDistance(m.distance_m)}
            {m.photo_count > 0 && (
              <span>
                <Icon name="camera" size={12} />
                {m.photo_count}
              </span>
            )}
            {(m.confirms > 0 || m.disputes > 0) && (
              <span>
                <Icon name="check" size={12} />
                {m.confirms}
                <Icon name="alert" size={12} />
                {m.disputes}
              </span>
            )}
            {m.mine && <span className="mk-mine">我的</span>}
          </span>
        </span>
        <StatusStamp status={m.status} disputed={m.disputed} />
      </button>
    </li>
  )
}

/** 侧栏卡片：附近的共享标注与自己提交的标注，点开看详情、投票、补充照片、撤回。 */
export function MarkingPanel({
  config,
  nearby,
  loading,
  error,
  radiusM,
  selectedId,
  onSelect,
  onCreate,
  onChanged,
  onToast,
  onLocate,
  revision,
}: Props) {
  const [tab, setTab] = useState<Tab>('nearby')
  const [mine, setMine] = useState<Marking[] | null>(null)
  const [mineError, setMineError] = useState<string | null>(null)

  useEffect(() => {
    if (tab !== 'mine') return
    let cancelled = false
    fetchMyMarkings()
      .then((items) => {
        if (!cancelled) {
          setMine(items)
          setMineError(null)
        }
      })
      .catch((err: Error) => {
        if (!cancelled) setMineError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [tab, revision])

  const verified = nearby.filter((m) => m.status === 'verified').length
  const pending = nearby.length - verified
  const ownNearby = nearby.filter((m) => m.mine).length

  return (
    <section className="card mk-panel" aria-label="共享标注">
      <div className="mk-panel-head">
        <h2 className="card-title">共享标注</h2>
        <button type="button" className="mk-btn primary small" onClick={onCreate}>
          <Icon name="plus" size={14} />
          新建
        </button>
      </div>

      {selectedId !== null ? (
        <MarkingDetailView
          key={selectedId}
          id={selectedId}
          config={config}
          onBack={() => onSelect(null)}
          onChanged={onChanged}
          onToast={onToast}
          onLocate={onLocate}
        />
      ) : (
        <>
          <p className="hint">
            地图数据不全的地方，你的一条标注能帮到后来的人。已核实的标注附近所有人分析时自动计入，
            待核实的只作为建议。
          </p>
          <div className="mk-tabs" role="tablist" aria-label="标注列表">
            <button
              type="button"
              role="tab"
              aria-selected={tab === 'nearby'}
              className={tab === 'nearby' ? 'on' : ''}
              onClick={() => setTab('nearby')}
            >
              附近 <small>{nearby.length}</small>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === 'mine'}
              className={tab === 'mine' ? 'on' : ''}
              onClick={() => setTab('mine')}
            >
              我提交的 {mine && <small>{mine.length}</small>}
            </button>
          </div>

          {tab === 'nearby' && (
            <>
              {nearby.length > 0 && (
                <p className="mk-summary">
                  中心 {(radiusM / 1000).toFixed(1)} 公里内：已核实 <b>{verified}</b> · 待核实{' '}
                  <b>{pending}</b>
                  {ownNearby > 0 && (
                    <>
                      {' '}
                      · 我的 <b>{ownNearby}</b>
                    </>
                  )}
                </p>
              )}
              {error ? (
                <p className="mk-error-line">{error}</p>
              ) : loading && nearby.length === 0 ? (
                <div className="mk-skeleton" />
              ) : nearby.length === 0 ? (
                <div className="mk-empty">
                  <Icon name="pin" size={22} />
                  <p>附近还没有标注。</p>
                  <button type="button" className="mk-btn" onClick={onCreate}>
                    标注第一处
                  </button>
                </div>
              ) : (
                <ul className="mk-list">
                  {nearby.map((m) => (
                    <MarkingRow key={m.id} m={m} selected={false} onClick={() => onSelect(m.id)} />
                  ))}
                </ul>
              )}
            </>
          )}

          {tab === 'mine' && (
            <>
              {mineError ? (
                <p className="mk-error-line">{mineError}</p>
              ) : mine === null ? (
                <div className="mk-skeleton" />
              ) : mine.length === 0 ? (
                <div className="mk-empty">
                  <Icon name="edit" size={22} />
                  <p>这台浏览器还没有提交过标注。</p>
                </div>
              ) : (
                <ul className="mk-list">
                  {mine.map((m) => (
                    <MarkingRow key={m.id} m={m} selected={false} showDate onClick={() => onSelect(m.id)} />
                  ))}
                </ul>
              )}
              <p className="hint">
                修改与撤回凭据只存在这台浏览器里；清除浏览器数据后只能查看，不能再修改。
              </p>
            </>
          )}
        </>
      )}
    </section>
  )
}
