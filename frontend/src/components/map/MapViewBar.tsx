import { useEffect, useId, useRef, useState } from 'react'
import type { ResultView, ViewSwitch } from '../../lib/resultView'
import { Icon } from '../markings/Icon'
import type { LayerState } from './context'

export interface LayerAvailability {
  innerRings: boolean
  /** 有网格结果才有热力与盲区方格 */
  grid: boolean
  /** 有编号的灰色区域数 */
  regions: number
  /** 附近共享标注数 */
  markings: number
  /** 复测疑似点与工地候选数 */
  clues: number
}

interface Props {
  layers: LayerState
  onLayers: (next: LayerState) => void
  available: LayerAvailability
  blindCategory: string
  blindCategories: string[]
  onBlindCategory: (name: string) => void
  viewSwitch: ViewSwitch
  resultView: ResultView
  onResultView: (view: ResultView) => void
  onRerun: (mode: 'auto' | 'none') => void
  busy: boolean
}

interface Row {
  key: keyof LayerState
  label: string
  note?: string
  disabled?: boolean
}

/**
 * 地图右上角：「纯算法 / 含标注」切换与图层开关。
 * 图层按「基础 / 诊断 / 标注」分组，没有数据的层直接不列，默认只开最常用的几层。
 */
export function MapViewBar({
  layers,
  onLayers,
  available,
  blindCategory,
  blindCategories,
  onBlindCategory,
  viewSwitch,
  resultView,
  onResultView,
  onRerun,
  busy,
}: Props) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)
  const panelId = useId()

  // 点面板外面或按 Esc 收起
  useEffect(() => {
    if (!open) return
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const groups: { title: string; rows: Row[] }[] = [
    {
      title: '基础',
      rows: [
        ...(available.innerRings ? [{ key: 'innerRings' as const, label: '5 / 10 分钟内圈' }] : []),
        {
          key: 'heat',
          label: '步行耗时热力',
          disabled: !available.grid,
          note: available.grid ? undefined : '这份结果没有网格',
        },
        { key: 'outsidePlaces', label: '圈外的设施' },
      ],
    },
    {
      title: '诊断',
      rows: [
        {
          key: 'blind',
          label: '盲区方格',
          disabled: !available.grid,
          note: available.grid ? '逐格实测步行 1 公里' : '这份结果没有网格',
        },
        {
          key: 'regions',
          label: '灰色区域编号与外框',
          disabled: available.regions === 0,
          note:
            available.regions === 0
              ? '这份结果没有连片的灰色区域'
              : blindCategory === 'all'
                ? `${available.regions} 片`
                : '只在「缺任一类」时显示',
        },
      ],
    },
    {
      title: '标注',
      rows: [
        {
          key: 'markings',
          label: '共享标注',
          note: available.markings > 0 ? `附近 ${available.markings} 条` : '附近还没有',
        },
        ...(available.clues > 0
          ? [{ key: 'clues' as const, label: '复测疑似点与工地候选', note: `${available.clues} 处` }]
          : []),
      ],
    },
  ]
  const shown = groups.flatMap((g) => g.rows).filter((r) => !r.disabled && layers[r.key]).length

  const algorithmActive = resultView === 'algorithm'
  const pick = (view: ResultView) => {
    if (view === resultView) return
    if (viewSwitch.kind === 'instant') onResultView(view)
    else if (viewSwitch.kind === 'rerun') onRerun(viewSwitch.to)
  }
  const rerunTitle =
    viewSwitch.kind === 'rerun'
      ? viewSwitch.to === 'auto'
        ? '重新计算并叠加附近标注（已测过的点对走缓存）'
        : '这份结果没有保存纯算法的明细，要按纯算法重新计算（大部分走缓存）'
      : undefined

  return (
    <div className="map-viewbar" ref={rootRef}>
      <div className="map-viewbar-row">
        {viewSwitch.kind !== 'none' && (
          <div className="view-switch floating" role="group" aria-label="地图显示哪种结果">
            <button
              type="button"
              aria-pressed={algorithmActive}
              disabled={busy && !algorithmActive}
              title={!algorithmActive ? rerunTitle : undefined}
              onClick={() => pick('algorithm')}
            >
              纯算法
            </button>
            <button
              type="button"
              aria-pressed={!algorithmActive}
              disabled={busy && algorithmActive}
              title={algorithmActive ? rerunTitle : undefined}
              onClick={() => pick('markings')}
            >
              含标注
            </button>
          </div>
        )}
        <button
          type="button"
          className="layer-btn floating"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((v) => !v)}
        >
          <Icon name="layers" size={15} />
          图层
          <b>{shown}</b>
        </button>
      </div>
      {viewSwitch.kind === 'instant' && algorithmActive && (
        <p className="view-caption floating">地图显示纯算法结果；侧栏仍是含标注的报告</p>
      )}
      {open && (
        <div id={panelId} className="layer-panel floating" role="group" aria-label="图层">
          {groups.map((g) => (
            <section key={g.title}>
              <h4>{g.title}</h4>
              {g.rows.map((r) => (
                <div key={r.key}>
                  <label className={r.disabled ? 'layer-row disabled' : 'layer-row'}>
                    <input
                      type="checkbox"
                      checked={!r.disabled && layers[r.key]}
                      disabled={r.disabled}
                      onChange={() => onLayers({ ...layers, [r.key]: !layers[r.key] })}
                    />
                    <span>
                      {r.label}
                      {r.note && <small>{r.note}</small>}
                    </span>
                  </label>
                  {r.key === 'blind' && !r.disabled && layers.blind && blindCategories.length > 0 && (
                    <div className="layer-chips" role="radiogroup" aria-label="盲区按品类">
                      {['all', ...blindCategories].map((name) => (
                        <button
                          key={name}
                          type="button"
                          role="radio"
                          aria-checked={blindCategory === name}
                          className={blindCategory === name ? 'on' : ''}
                          onClick={() => onBlindCategory(name)}
                        >
                          {name === 'all' ? '缺任一类' : name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </section>
          ))}
        </div>
      )}
    </div>
  )
}
