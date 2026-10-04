import { type CSSProperties, useEffect, useMemo, useRef, useState } from 'react'
import { ApiError, createMarking, stageMarkingPhoto } from '../../api'
import {
  TYPE_META,
  circleHitsPath,
  daysFromNow,
  formatDistance,
  metersBetween,
  polygonAreaM2,
  polygonProblem,
  preparePhoto,
  saveEditToken,
  type LatLng,
  type PreparedPhoto,
} from '../../lib/markings'
import type {
  Marking,
  MarkingConfig,
  MarkingInput,
  MarkingType,
  Place,
  RayMetric,
} from '../../types'
import type { ComposerPreset } from '../map/context'
import { emptyDraft, type Draft, type DraftHistory } from './draft'
import { Icon, TypeBadge } from './Icon'

export type ComposerStep = 'type' | 'why' | 'place' | 'details'

interface Props {
  config: MarkingConfig
  history: DraftHistory
  source: 'user' | 'recheck' | 'poi' | 'agent_plan'
  /** 步骤由外面持有：地图只在「位置」这一步把点击交给草图 */
  step: ComposerStep
  onStep: (step: ComposerStep) => void
  /** 从地图上点进来时带好的类别、原因与来源说明 */
  preset?: ComposerPreset | null
  places: Place[]
  rays: RayMetric[]
  center: LatLng
  onCreated: (marking: Marking) => void
  onFocusExisting: (marking: Marking) => void
  onClose: () => void
}

/** 设施失效：从一个位置出发时，吸附到 80 米内最近的地图设施。 */
const SNAP_M = 80

/**
 * 换类型时把已经点好的位置带过去：从「这里」点进灰色区域、再在「为什么缺」里改成围挡，
 * 用户点过的那个位置不该丢。
 */
function carryDraft(from: Draft, type: MarkingType, places: Place[]): Draft {
  const anchor = from.point ?? from.vertices[0] ?? null
  const next = emptyDraft(type, from.radius)
  if (type === 'gray_area') {
    return { ...next, vertices: from.type === 'gray_area' ? from.vertices : anchor ? [anchor] : [] }
  }
  if (!anchor) return next
  if (type === 'facility_missing') {
    const nearest = places
      .filter((p) => p.source !== 'user')
      .map((p) => ({ p, d: metersBetween(anchor, p) }))
      .sort((a, b) => a.d - b.d)[0]
    return nearest && nearest.d <= SNAP_M
      ? { ...next, place: nearest.p, point: { lat: nearest.p.lat, lng: nearest.p.lng } }
      : next
  }
  return { ...next, point: anchor }
}

const TYPE_ORDER: MarkingType[] = ['closure', 'facility_missing', 'facility_extra', 'gray_area']

/** 标灰色区域之前先问「为什么缺」：能转成原因的，引导到更有用的标注类型。 */
const WHY: {
  key: string
  label: string
  hint: string
  to: MarkingType
  reason?: string
}[] = [
  {
    key: 'missing',
    label: '地图上有，但关了或不对外',
    hint: '改标「设施失效」：点选那家设施，重算时会把它剔除',
    to: 'facility_missing',
  },
  {
    key: 'blocked',
    label: '路走不通，要绕很远',
    hint: '改标「围挡 / 封路」：标出挡路的地方，重算时路线在那里截断',
    to: 'closure',
  },
  {
    key: 'capacity',
    label: '设施在，但不够用',
    hint: '学位满、只开半天……这类问题算法从地图上看不出来',
    to: 'gray_area',
    reason: 'capacity',
  },
  { key: 'quality', label: '服务质量差', hint: '作为人工灰色区域记录下来', to: 'gray_area', reason: 'quality' },
  {
    key: 'outside',
    label: '在分析范围之外',
    hint: '15 分钟步行圈以外、网格没覆盖到的地方',
    to: 'gray_area',
    reason: 'outside_grid',
  },
  { key: 'other', label: '其他原因', hint: '需要用一句话说明', to: 'gray_area', reason: 'other' },
]

const NOTE_PLACEHOLDER: Record<MarkingType, string> = {
  closure: '如：景泰路西段施工，人行道整段封闭',
  facility_missing: '如：去年底已关门，招牌还在',
  facility_extra: '如：村委会旁，工作日上午开门',
  gray_area: '如：小学学位已满，新生要去 3 公里外',
}

const PLACE_GLYPH: Record<string, string> = {
  生鲜采买: '菜',
  医药: '药',
  基础教育: '学',
  基础医疗: '医',
  养老服务: '养',
  文体休闲: '文',
}

function isTyping(target: EventTarget | null) {
  const el = target as HTMLElement | null
  return !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)
}

export function MarkingComposer({
  config,
  history,
  source,
  step,
  onStep,
  preset = null,
  places,
  rays,
  center,
  onCreated,
  onFocusExisting,
  onClose,
}: Props) {
  const { draft } = history
  const meta = TYPE_META[draft.type]
  const [kind, setKind] = useState('construction')
  const [category, setCategory] = useState(
    preset?.category && config.categories.includes(preset.category)
      ? preset.category
      : (config.categories[0] ?? ''),
  )
  const [name, setName] = useState(preset?.name ?? '')
  const [reason, setReason] = useState(preset?.reason ?? '')
  const [categories, setCategories] = useState<string[]>(
    (preset?.categories ?? []).filter((c) => config.categories.includes(c)),
  )
  // 退出要点两下：画了一半、写了一半的东西不该被误点一下就丢掉
  const [confirmExit, setConfirmExit] = useState(false)
  const exitTimerRef = useRef(0)
  useEffect(() => () => window.clearTimeout(exitTimerRef.current), [])
  const [note, setNote] = useState('')
  const [ttl, setTtl] = useState<number | null>(null)
  const [photos, setPhotos] = useState<PreparedPhoto[]>([])
  const [photoError, setPhotoError] = useState<string | null>(null)
  const [preparing, setPreparing] = useState(false)
  const [phase, setPhase] = useState<{ label: string; progress?: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [duplicate, setDuplicate] = useState<Marking | null>(null)
  const [query, setQuery] = useState('')
  const fileRef = useRef<HTMLInputElement>(null)
  // 已经传上去的照片：预览地址 → 预传 ID。提交被拒（比如附近有重复）后再提交不必重传
  const stagedRef = useRef(new Map<string, string>())
  // 现场照片必填：管理员核实要靠它
  const minPhotos = config.photo.required ? Math.max(1, config.photo.min_per_marking ?? 1) : 0
  const photosRef = useRef(photos)
  useEffect(() => {
    photosRef.current = photos
  }, [photos])

  // 关掉面板时释放预览图占用的内存
  useEffect(() => () => photosRef.current.forEach((p) => URL.revokeObjectURL(p.url)), [])

  // 草图的撤销 / 重做快捷键，只在「位置」这一步；正在输入文字时让给输入框自己的撤销
  const { undo, redo } = history
  const editingPlace = step === 'place'
  useEffect(() => {
    if (!editingPlace) return
    const onKey = (e: KeyboardEvent) => {
      if (isTyping(e.target) || !(e.ctrlKey || e.metaKey)) return
      const key = e.key.toLowerCase()
      if (key === 'z' && !e.shiftKey) {
        e.preventDefault()
        undo()
      } else if ((key === 'z' && e.shiftKey) || key === 'y') {
        e.preventDefault()
        redo()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [undo, redo, editingPlace])

  const [radiusLo, radiusHi] = config.limits.closure_radius_m
  const defaultTtl =
    draft.type === 'closure'
      ? (config.closure_kinds[kind]?.ttl_days ?? 60)
      : (config.type_ttl_days[draft.type] ?? 180)
  const days = ttl ?? defaultTtl
  const ttlOptions = Array.from(new Set([7, 30, 60, 90, 180, 365, defaultTtl])).sort((a, b) => a - b)

  const polygonIssue =
    draft.type === 'gray_area'
      ? polygonProblem(
          draft.vertices,
          config.limits.polygon_max_vertices,
          config.limits.polygon_max_area_km2,
        )
      : null
  const area = draft.type === 'gray_area' ? polygonAreaM2(draft.vertices) : 0

  const routeHits = useMemo(() => {
    if (draft.type !== 'closure' || !draft.point) return null
    const withPaths = rays.filter((r) => (r.route_path?.length ?? 0) >= 2)
    if (withPaths.length === 0) return null
    return withPaths.filter((r) => circleHitsPath(draft.point!, draft.radius, r.route_path!)).length
  }, [draft, rays])

  const candidates = useMemo(() => {
    const origin = draft.point ?? center
    const q = query.trim()
    return places
      .filter((p) => p.source !== 'user')
      .filter((p) => !q || p.name.includes(q) || p.category.includes(q))
      .map((p) => ({ p, d: metersBetween(origin, p) }))
      .sort((a, b) => a.d - b.d)
      .slice(0, 30)
  }, [places, draft.point, center, query])

  const placeReady =
    draft.type === 'gray_area'
      ? polygonIssue === null
      : draft.type === 'facility_missing'
        ? draft.place !== null
        : draft.point !== null

  const noteRequired = draft.type === 'gray_area' && reason === 'other'
  const detailProblem = (() => {
    if (draft.type === 'facility_missing' && !reason) return '请选择失效原因'
    if (draft.type === 'facility_extra' && !name.trim()) return '请填写设施名称'
    if (draft.type === 'facility_extra' && !category) return '请选择设施类别'
    if (draft.type === 'gray_area' && categories.length === 0) return '请选择缺哪类设施'
    if (draft.type === 'gray_area' && !reason) return '请选择原因'
    if (noteRequired && !note.trim()) return '选「其他原因」时，请用一句话说明'
    if (photos.length < minPhotos) return '请至少添加一张现场照片，管理员要靠它核实'
    return null
  })()

  function chooseType(type: MarkingType, presetReason?: string) {
    history.reset(carryDraft(draft, type, places))
    setReason(presetReason ?? '')
    setError(null)
    setDuplicate(null)
    onStep(type === 'gray_area' && !presetReason ? 'why' : 'place')
  }

  const hasProgress =
    draft.point !== null ||
    draft.vertices.length > 0 ||
    note.trim() !== '' ||
    name.trim() !== '' ||
    photos.length > 0

  function requestExit() {
    if (!hasProgress || confirmExit || phase) {
      if (!phase) onClose()
      return
    }
    setConfirmExit(true)
    window.clearTimeout(exitTimerRef.current)
    exitTimerRef.current = window.setTimeout(() => setConfirmExit(false), 3000)
  }

  // 顶部模式条：现在在做什么、下一步点哪里
  const bar = (() => {
    if (step === 'type') return { title: '新建共享标注', hint: '先在右侧选要标注什么' }
    if (step === 'why') return { title: meta.label, hint: '在右侧选一个最接近的原因' }
    if (step === 'details') return { title: meta.label, hint: '位置已定，在右侧补充说明后提交' }
    switch (draft.type) {
      case 'closure':
        return {
          title: meta.label,
          hint: draft.point ? '拖动圆边上的白点调半径，再点地图可以换位置' : '点地图选围挡的圆心',
        }
      case 'facility_extra':
        return {
          title: meta.label,
          hint: draft.point ? '已放好，点地图可以挪到设施门口' : '点地图放到设施门口',
        }
      case 'facility_missing':
        return {
          title: meta.label,
          hint: draft.place ? `已选「${draft.place.name}」，点别的设施可以换` : '点地图上那家设施，或在右侧列表里选',
        }
      case 'gray_area':
        return {
          title: meta.label,
          hint: `沿边界依次点地图，已 ${draft.vertices.length} 个顶点，自动闭合`,
        }
    }
  })()

  async function addFiles(files: FileList | null) {
    if (!files || files.length === 0) return
    setPhotoError(null)
    const room = config.photo.max_per_uploader - photos.length
    const list = Array.from(files).slice(0, Math.max(0, room))
    if (files.length > room) {
      setPhotoError(`每条标注你最多上传 ${config.photo.max_per_uploader} 张照片`)
    }
    setPreparing(true)
    const ready: PreparedPhoto[] = []
    for (const file of list) {
      try {
        ready.push(await preparePhoto(file, config.photo.max_bytes))
      } catch (err) {
        setPhotoError(err instanceof Error ? err.message : String(err))
      }
    }
    setPreparing(false)
    setPhotos((prev) => [...prev, ...ready])
    if (fileRef.current) fileRef.current.value = ''
  }

  function removePhoto(i: number) {
    setPhotos((prev) => {
      URL.revokeObjectURL(prev[i].url)
      // 已经传上去的那份留在服务端的预传区，一小时后自动清掉
      stagedRef.current.delete(prev[i].url)
      return prev.filter((_, j) => j !== i)
    })
  }

  /** 把还没传的照片逐张传上去，返回全部预传 ID（顺序与界面一致）。 */
  async function stageAll(): Promise<string[]> {
    const ids: string[] = []
    for (let i = 0; i < photos.length; i++) {
      const p = photos[i]
      let id = stagedRef.current.get(p.url)
      if (!id) {
        const label = `上传照片 ${i + 1} / ${photos.length}`
        setPhase({ label, progress: 0 })
        try {
          id = (await stageMarkingPhoto(p.blob, (r) => setPhase({ label, progress: r }))).id
        } catch (err) {
          const reason = err instanceof Error ? err.message : String(err)
          throw new Error(`第 ${i + 1} 张照片没传上去：${reason}`)
        }
        stagedRef.current.set(p.url, id)
      }
      ids.push(id)
    }
    return ids
  }

  function buildInput(force: boolean, photoIds: string[]): MarkingInput {
    const base = { note: note.trim(), expires_in_days: days, source, force, photos: photoIds }
    switch (draft.type) {
      case 'closure':
        return {
          ...base,
          type: 'closure',
          kind,
          lat: draft.point!.lat,
          lng: draft.point!.lng,
          radius_m: draft.radius,
        }
      case 'facility_missing':
        return {
          ...base,
          type: 'facility_missing',
          category: draft.place!.category,
          name: draft.place!.name,
          lat: draft.place!.lat,
          lng: draft.place!.lng,
          reason,
        }
      case 'facility_extra':
        return {
          ...base,
          type: 'facility_extra',
          category,
          name: name.trim(),
          lat: draft.point!.lat,
          lng: draft.point!.lng,
        }
      case 'gray_area':
        return {
          ...base,
          type: 'gray_area',
          polygon: draft.vertices.map((v) => [v.lng, v.lat]),
          reason,
          categories,
        }
    }
  }

  /**
   * 先传照片、再新建：服务端在新建的同一个事务里把照片挂上，要么标注和照片一起成功，
   * 要么都不生效——不会再有「标注建好了、照片没传上」的半截状态。
   */
  async function submit(force = false) {
    if (phase || detailProblem || !placeReady) return
    setError(null)
    setDuplicate(null)
    try {
      for (let attempt = 0; ; attempt++) {
        const ids = await stageAll()
        setPhase({ label: '正在提交…' })
        try {
          const { marking, edit_token } = await createMarking(buildInput(force, ids))
          saveEditToken(marking.id, edit_token)
          onCreated(marking)
          return
        } catch (err) {
          // 预传的照片过期了（在这一步停了一个多小时）：重新传一遍再提交，只重试一次
          if (err instanceof ApiError && err.code === 'photo_expired' && attempt === 0) {
            stagedRef.current.clear()
            continue
          }
          throw err
        }
      }
    } catch (err) {
      if (err instanceof ApiError && err.code === 'duplicate' && err.detail.existing) {
        setDuplicate(err.detail.existing)
      } else {
        setError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setPhase(null)
    }
  }

  const stepIndex = step === 'type' || step === 'why' ? 0 : step === 'place' ? 1 : 2
  const accent = { '--mk-accent': meta.color, '--mk-soft': meta.soft } as CSSProperties
  const kicker = preset?.context ?? (source !== 'user' ? `来自${config.sources[source]}` : null)

  return (
    <>
      <div className="mk-modebar" role="status" aria-live="polite" style={accent}>
        <span className="mk-modebar-mark">
          <Icon name={step === 'type' ? 'pin' : draft.type} size={16} />
        </span>
        <span className="mk-modebar-text">
          <b>正在标注 · {bar.title}</b>
          <em>{bar.hint}</em>
        </span>
        <span className="mk-modebar-actions">
          {step === 'place' && (
            <>
              <button type="button" disabled={!history.canUndo} onClick={history.undo} title="撤销（Ctrl+Z）">
                <Icon name="undo" size={14} />
                撤销
              </button>
              <button type="button" disabled={!history.canRedo} onClick={history.redo} title="重做（Ctrl+Shift+Z）">
                <Icon name="redo" size={14} />
                重做
              </button>
            </>
          )}
          <button
            type="button"
            className={confirmExit ? 'exit confirm' : 'exit'}
            disabled={phase !== null}
            onClick={requestExit}
          >
            {confirmExit ? '确定退出？已画的会丢掉' : '退出标注'}
          </button>
        </span>
      </div>
    <section className="mk-composer floating" aria-label="新建共享标注" style={accent}>
      <header className="mk-composer-head">
        <div>
          <p className="mk-kicker">共享标注{kicker ? ` · ${kicker}` : ''}</p>
          <h2>{step === 'type' ? '你想标注什么？' : meta.label}</h2>
        </div>
        <button type="button" className="mk-icon-btn" aria-label="退出标注" onClick={requestExit}>
          <Icon name="close" />
        </button>
      </header>

      <ol className="mk-steps" aria-label="步骤">
        {['类型', '位置', '说明'].map((label, i) => (
          <li
            key={label}
            className={i < stepIndex ? 'done' : i === stepIndex ? 'active' : ''}
            aria-current={i === stepIndex ? 'step' : undefined}
          >
            <i>{i < stepIndex ? <Icon name="check" size={11} /> : i + 1}</i>
            {label}
          </li>
        ))}
      </ol>

      <div className="mk-composer-body">
        {step === 'type' && (
          <div className="mk-type-grid">
            {TYPE_ORDER.map((type) => {
              const m = TYPE_META[type]
              return (
                <button
                  key={type}
                  type="button"
                  className="mk-type-card"
                  style={{ '--mk-accent': m.color, '--mk-soft': m.soft } as CSSProperties}
                  onClick={() => chooseType(type)}
                >
                  <TypeBadge type={type} color={m.color} soft={m.soft} size={38} />
                  <b>{m.label}</b>
                  <span>{m.hint}</span>
                </button>
              )
            })}
          </div>
        )}

        {step === 'why' && (
          <div className="mk-why">
            <p className="mk-lead">这里为什么缺设施？选一个最接近的原因。</p>
            <ul>
              {WHY.map((w) => (
                <li key={w.key}>
                  <button type="button" onClick={() => chooseType(w.to, w.reason)}>
                    <span>
                      <b>{w.label}</b>
                      <em>{w.hint}</em>
                    </span>
                    {w.to !== 'gray_area' && (
                      <span className="mk-why-to" style={{ color: TYPE_META[w.to].color }}>
                        改标{TYPE_META[w.to].short}
                      </span>
                    )}
                    <Icon name="chevron" size={14} />
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {step === 'place' && (
          <div className="mk-place">
            <p className="mk-instruction">
              <Icon name="pin" size={15} />
              {draft.type === 'closure' &&
                (draft.point
                  ? '拖动地图上圆边的白点，或用下面的滑块，让圆盖住围挡范围'
                  : '在地图上点一下挡路的位置，再调半径盖住围挡范围')}
              {draft.type === 'facility_extra' &&
                (draft.point && preset?.point
                  ? source === 'agent_plan'
                    ? '先放在了 Agent Plan 给的位置。到现场确认后，点地图把它挪到门口'
                    : '先放在了你点的位置。点地图可以挪到设施门口'
                  : '在地图上点一下设施的位置（尽量放在门口）')}
              {draft.type === 'facility_missing' && '点地图上那家设施的标记，或从下面的列表里选'}
              {draft.type === 'gray_area' && '沿着区域边界依次点地图，至少 3 个点，自动闭合'}
            </p>

            {(draft.type === 'closure' || draft.type === 'facility_extra') && (
              <div className={draft.point ? 'mk-point set' : 'mk-point'}>
                {draft.point ? (
                  <>
                    <Icon name="check" size={14} />
                    已放置 · {draft.point.lat.toFixed(5)}, {draft.point.lng.toFixed(5)}
                  </>
                ) : (
                  '还没放置'
                )}
              </div>
            )}

            {draft.type === 'closure' && (
              <>
                <label className="mk-field">
                  <span className="mk-label">
                    半径 <b>{draft.radius} 米</b>
                  </span>
                  <input
                    type="range"
                    min={radiusLo}
                    max={radiusHi}
                    step={5}
                    value={draft.radius}
                    onChange={(e) => history.push({ ...draft, radius: Number(e.target.value) })}
                  />
                </label>
                <p className="mk-preview">
                  {routeHits === null
                    ? '这份结果没有保存各方向的步行路线，提交后重新计算才能看到影响。'
                    : routeHits > 0
                      ? `按现在的位置和半径，会挡住 ${routeHits} 个方向的步行路线。`
                      : '按现在的位置和半径，不会挡住任何一个方向的步行路线。'}
                </p>
              </>
            )}

            {draft.type === 'facility_missing' && (
              <div className="mk-picker">
                <input
                  type="text"
                  value={query}
                  placeholder="搜索设施名称或类别"
                  aria-label="搜索设施"
                  onChange={(e) => setQuery(e.target.value)}
                />
                {candidates.length === 0 ? (
                  <p className="mk-empty-line">
                    {places.length === 0
                      ? '这份结果里没有设施列表。先做一次含设施检索的分析。'
                      : '没有匹配的设施。'}
                  </p>
                ) : (
                  <ul role="listbox" aria-label="附近设施">
                    {candidates.map(({ p, d }) => {
                      const selected =
                        draft.place?.name === p.name &&
                        draft.place?.lat === p.lat &&
                        draft.place?.lng === p.lng
                      return (
                        <li key={`${p.category}-${p.name}-${p.lat}-${p.lng}`}>
                          <button
                            type="button"
                            role="option"
                            aria-selected={selected}
                            className={selected ? 'selected' : ''}
                            onClick={() =>
                              history.push({ ...draft, place: p, point: { lat: p.lat, lng: p.lng } })
                            }
                          >
                            <i>{PLACE_GLYPH[p.category] ?? '·'}</i>
                            <span>
                              <b>{p.name}</b>
                              <em>{p.category}</em>
                            </span>
                            <small>{formatDistance(d)}</small>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </div>
            )}

            {draft.type === 'gray_area' && (
              <div className={polygonIssue ? 'mk-point' : 'mk-point set'}>
                {draft.vertices.length} 个顶点
                {draft.vertices.length >= 3 && ` · 约 ${(area / 1e6).toFixed(3)} km²`}
                {polygonIssue && <em>{polygonIssue}</em>}
              </div>
            )}

            <div className="mk-history-bar" role="toolbar" aria-label="草图操作">
              <span className="mk-history-tip">撤销 / 重做在顶部，也可以按 Ctrl+Z</span>
              <button
                type="button"
                disabled={!draft.point && draft.vertices.length === 0}
                onClick={() => history.push(emptyDraft(draft.type, draft.radius))}
              >
                <Icon name="trash" size={14} />
                清空
              </button>
            </div>
          </div>
        )}

        {step === 'details' && (
          <div className="mk-details">
            {draft.type === 'closure' && (
              <fieldset className="mk-field">
                <legend className="mk-label">种类</legend>
                <div className="mk-pills">
                  {Object.entries(config.closure_kinds).map(([k, v]) => (
                    <button
                      key={k}
                      type="button"
                      className={kind === k ? 'on' : ''}
                      aria-pressed={kind === k}
                      onClick={() => {
                        setKind(k)
                        setTtl(null)
                      }}
                    >
                      {v.label}
                    </button>
                  ))}
                </div>
              </fieldset>
            )}

            {draft.type === 'facility_missing' && draft.place && (
              <>
                <div className="mk-facility">
                  <i>{PLACE_GLYPH[draft.place.category] ?? '·'}</i>
                  <span>
                    <b>{draft.place.name}</b>
                    <em>{draft.place.category} · 地图上的设施</em>
                  </span>
                </div>
                <fieldset className="mk-field">
                  <legend className="mk-label">怎么了</legend>
                  <div className="mk-pills">
                    {Object.entries(config.missing_reasons).map(([k, label]) => (
                      <button
                        key={k}
                        type="button"
                        className={reason === k ? 'on' : ''}
                        aria-pressed={reason === k}
                        onClick={() => setReason(k)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </fieldset>
              </>
            )}

            {draft.type === 'facility_extra' && (
              <>
                <fieldset className="mk-field">
                  <legend className="mk-label">类别</legend>
                  <div className="mk-pills">
                    {config.categories.map((c) => (
                      <button
                        key={c}
                        type="button"
                        className={category === c ? 'on' : ''}
                        aria-pressed={category === c}
                        onClick={() => setCategory(c)}
                      >
                        {c}
                      </button>
                    ))}
                  </div>
                </fieldset>
                <label className="mk-field">
                  <span className="mk-label">设施名称</span>
                  <input
                    type="text"
                    value={name}
                    maxLength={config.limits.name_max}
                    placeholder="如：桃源村卫生室"
                    onChange={(e) => setName(e.target.value)}
                  />
                </label>
              </>
            )}

            {draft.type === 'gray_area' && (
              <>
                <fieldset className="mk-field">
                  <legend className="mk-label">缺哪类设施（可多选）</legend>
                  <div className="mk-pills">
                    {config.categories.map((c) => {
                      const on = categories.includes(c)
                      return (
                        <button
                          key={c}
                          type="button"
                          className={on ? 'on' : ''}
                          aria-pressed={on}
                          onClick={() =>
                            setCategories(on ? categories.filter((x) => x !== c) : [...categories, c])
                          }
                        >
                          {c}
                        </button>
                      )
                    })}
                  </div>
                </fieldset>
                <fieldset className="mk-field">
                  <legend className="mk-label">原因</legend>
                  <div className="mk-pills">
                    {Object.entries(config.gray_reasons).map(([k, label]) => (
                      <button
                        key={k}
                        type="button"
                        className={reason === k ? 'on' : ''}
                        aria-pressed={reason === k}
                        onClick={() => setReason(k)}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </fieldset>
              </>
            )}

            <label className="mk-field">
              <span className="mk-label">
                说明{noteRequired ? '' : '（选填）'}
                <small>
                  {note.length} / {config.limits.note_max}
                </small>
              </span>
              <textarea
                rows={2}
                value={note}
                maxLength={config.limits.note_max}
                placeholder={NOTE_PLACEHOLDER[draft.type]}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>

            <label className="mk-field mk-inline">
              <span className="mk-label">有效期</span>
              <select value={days} onChange={(e) => setTtl(Number(e.target.value))}>
                {ttlOptions.map((d) => (
                  <option key={d} value={d}>
                    {d} 天{d === defaultTtl ? '（默认）' : ''}
                  </option>
                ))}
              </select>
              <small>至 {daysFromNow(days)}</small>
            </label>

            <div className="mk-field">
              <span className="mk-label">
                现场照片
                {minPhotos > 0 ? <b className="mk-required">必填</b> : '（选填）'}
                <small>
                  {photos.length} / {config.photo.max_per_uploader}
                </small>
              </span>
              <div className="mk-photo-row">
                {photos.map((p, i) => (
                  <figure key={p.url} className="mk-photo-chip">
                    <img src={p.url} alt={`待上传照片 ${i + 1}`} />
                    <button type="button" aria-label={`移除第 ${i + 1} 张`} onClick={() => removePhoto(i)}>
                      <Icon name="close" size={12} />
                    </button>
                  </figure>
                ))}
                {photos.length < config.photo.max_per_uploader && (
                  <label
                    className={[
                      'mk-photo-add',
                      preparing ? 'busy' : '',
                      photos.length < minPhotos ? 'needed' : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <input
                      ref={fileRef}
                      type="file"
                      accept="image/jpeg,image/png,image/webp,image/heic,image/heif"
                      multiple
                      onChange={(e) => void addFiles(e.target.files)}
                    />
                    <Icon name="camera" size={20} />
                    <span>{preparing ? '处理中…' : '添加'}</span>
                  </label>
                )}
              </div>
              <p className="mk-hint">
                {minPhotos > 0
                  ? '至少拍一张现场照片：管理员靠它核实，没有照片的标注无法审核。'
                  : '有照片的标注更容易被核实。'}
                照片会重新压缩并去掉拍摄地点等信息，请避开人脸和车牌。
              </p>
              {photoError && <p className="mk-error-line">{photoError}</p>}
            </div>

            <div className="mk-share-note">
              <Icon name="shield" size={16} />
              <p>
                提交后附近的用户都能看到它。<b>对你立即生效</b>
                ；其他人要等管理员审核核实后才会自动计入，在那之前只作为建议。
              </p>
            </div>
          </div>
        )}

        {duplicate && (
          <div className="mk-duplicate" role="alert">
            <p>
              <b>附近已有一条同类标注</b>：「{duplicate.title}」（{duplicate.confirms} 人确认）。
              说的是同一处的话，去确认它比再建一条更有用。
            </p>
            <div>
              <button type="button" className="mk-btn primary" onClick={() => onFocusExisting(duplicate)}>
                去看看它
              </button>
              <button type="button" className="mk-btn" onClick={() => void submit(true)}>
                不是同一处，仍然提交
              </button>
            </div>
          </div>
        )}
        {error && (
          <p className="mk-error-line" role="alert">
            {error}
          </p>
        )}
      </div>

      <footer className="mk-composer-foot">
        {step !== 'type' && (
          <button
            type="button"
            className="mk-btn"
            disabled={phase !== null}
            onClick={() =>
              onStep(
                step === 'details'
                  ? 'place'
                  : step === 'place' && draft.type === 'gray_area' && reason && WHY.some((w) => w.reason === reason)
                    ? 'why'
                    : 'type',
              )
            }
          >
            <Icon name="back" size={14} />
            上一步
          </button>
        )}
        <span className="mk-foot-spacer" />
        {step === 'place' && (
          <button type="button" className="mk-btn primary" disabled={!placeReady} onClick={() => onStep('details')}>
            下一步
            <Icon name="chevron" size={14} />
          </button>
        )}
        {step === 'details' && (
          <button
            type="button"
            className="mk-btn primary"
            disabled={phase !== null || detailProblem !== null || !placeReady || preparing}
            title={detailProblem ?? undefined}
            onClick={() => void submit(false)}
          >
            {phase ? phase.label : '提交标注'}
          </button>
        )}
      </footer>
      {phase?.progress !== undefined && (
        <div className="mk-progress" aria-hidden="true">
          <i style={{ width: `${Math.round(phase.progress * 100)}%` }} />
        </div>
      )}
      {step === 'details' && detailProblem && !phase && <p className="mk-foot-hint">{detailProblem}</p>}
    </section>
    </>
  )
}
