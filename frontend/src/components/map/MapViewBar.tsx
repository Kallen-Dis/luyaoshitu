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
  /** 网格交叉校核：确认缺口片数与显示的绕行线索段数；没有校核结果为 null */
  check: { gaps: number; clues: number; version?: number; rejection?: string } | null
  fusion?: { added: boolean; pending: boolean } | null
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
        ...(available.innerRings ? [{ key: 'innerRings' as const, label: '5 / 10 分钟参考内圈', note: '浅绿虚线，不叠加填充' }] : []),
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
    ...(available.fusion && available.check
      ? [{ title: '方法对照', rows: [
          { key: 'rayBaseline' as const, label: '原射线法范围', note: '同一次分析的原始估计，细虚线' },
          { key: 'checkGaps' as const, label: '原圈内超时证据', disabled: available.check.gaps === 0, note: available.check.gaps ? `${available.check.gaps} 片，红色斜线` : '没有确认的超时片' },
          { key: 'fusionAdded' as const, label: '融合后补回的范围', disabled: !available.fusion.added, note: available.fusion.added ? '原射线圈外新增的估计可达部分' : '没有补回范围' },
          { key: 'detourClues' as const, label: '绕行线索', disabled: available.check.clues === 0, note: '位置仅为线索，点开查看核验证据' },
          ...(available.fusion.pending ? [{ key: 'fusionPending' as const, label: '待核实区域', note: '浅琥珀点纹，点击查看原因' }] : []),
        ] }]
      : available.check
      ? [
          {
            title: '网格交叉校核',
            rows: [
              {
                key: 'checkGaps' as const,
                label: '射线圈里走不到的缺口',
                disabled: available.check.gaps === 0,
                note: available.check.gaps > 0 ? `确认 ${available.check.gaps} 片，斜线` : '没有确认的缺口',
              },
              { key: 'checkEstimate' as const, label: (available.check.version ?? 1) >= 2 ? '融合候选范围（未采用）' : '校核估计范围', note: (available.check.version ?? 1) >= 2 ? (available.check.rejection ? `未采用：${available.check.rejection}` : '仅作方法对照，不参与面积与评分，点划线') : '射线点加网格点重新估的边界，点划线' },
              {
                key: 'detourClues' as const,
                label: '绕行线索',
                disabled: available.check.clues === 0,
                note: available.check.clues > 0 ? `${available.check.clues} 段，位置约 ±50 米` : '没有达到显示阈值的线索',
              },
            ],
          },
        ]
      : []),
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
  const comparing = layers.rayBaseline

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
        <p className="view-caption floating">当前查看纯算法结果</p>
      )}
      {open && (
        <div id={panelId} className="layer-panel floating" role="group" aria-label="图层">
          {available.fusion && <div className="layer-method-head"><p><i aria-hidden="true" />主范围：校正后的步行范围</p><button type="button" aria-pressed={comparing} onClick={() => onLayers({ ...layers, rayBaseline: !comparing })}>{comparing ? '收起边界对照' : '查看方法对照'}</button></div>}
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
