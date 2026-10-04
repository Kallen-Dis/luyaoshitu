import { type CSSProperties, useCallback, useEffect, useRef, useState } from 'react'
import {
  deleteMarkingPhoto,
  fetchMarking,
  restoreMarking,
  retractMarking,
  revertMarking,
  updateMarking,
  uploadMarkingPhoto,
  voteMarking,
  type MarkingPatch,
} from '../../api'
import {
  STATUS_META,
  TYPE_META,
  daysFromNow,
  editToken,
  formatDate,
  formatDateTime,
  formatDistance,
  preparePhoto,
} from '../../lib/markings'
import type { Marking, MarkingConfig, MarkingDetail as Detail } from '../../types'
import { Icon, TypeBadge } from './Icon'
import { PhotoGallery } from './PhotoGallery'
import type { ToastSpec } from './UndoToast'

interface Props {
  id: number
  config: MarkingConfig
  onBack: () => void
  /** 标注有变化（投票、修改、撤回……），列表与地图需要刷新 */
  onChanged: (marking: Marking) => void
  onToast: (toast: Omit<ToastSpec, 'id'>) => void
  onLocate: (marking: Marking) => void
}

const ACTOR_LABEL: Record<string, string> = {
  author: '提交者',
  other: '其他用户',
  admin: '管理员',
  system: '系统',
}

export function StatusStamp({ status, disputed }: { status: Marking['status']; disputed?: boolean }) {
  const meta = STATUS_META[status]
  return (
    <span className={`mk-stamp tone-${meta.tone}`}>
      {status === 'verified' && <Icon name="check" size={11} />}
      {meta.label}
      {disputed && status !== 'verified' && <em>有争议</em>}
    </span>
  )
}

export function MarkingDetailView({ id, config, onBack, onChanged, onToast, onLocate }: Props) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [upload, setUpload] = useState<{ label: string; progress: number } | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const token = editToken(id)

  const load = useCallback(async () => {
    try {
      const d = await fetchMarking(id)
      setDetail(d)
      setError(null)
      return d
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      return null
    }
  }, [id])

  // 调用方以标注 ID 作 key 渲染：换一条就是新实例，状态不会串
  useEffect(() => {
    let cancelled = false
    fetchMarking(id)
      .then((d) => {
        if (!cancelled) setDetail(d)
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [id])

  async function act(label: string, fn: () => Promise<Marking>, toast?: Omit<ToastSpec, 'id'>) {
    setBusy(label)
    setError(null)
    try {
      const m = await fn()
      onChanged(m)
      await load()
      if (toast) onToast(toast)
      return m
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      return null
    } finally {
      setBusy(null)
    }
  }

  if (!detail) {
    return (
      <div className="mk-detail">
        <button type="button" className="mk-back" onClick={onBack}>
          <Icon name="back" size={14} />
          返回列表
        </button>
        {error ? <p className="mk-error-line">{error}</p> : <div className="mk-skeleton" />}
      </div>
    )
  }

  const meta = TYPE_META[detail.type]
  const active = detail.status === 'pending' || detail.status === 'verified'
  const canEdit = detail.mine && token !== null
  // 现场照片必填：作者不能把最后一张可见照片删掉（服务端同样拦着）；其他人删自己补的照片不受限
  const lastPhotoLocked =
    (detail.mine || canEdit) && detail.photos.length <= (config.photo.min_per_marking ?? 1)
  const spec = detail.spec
  const previousVote = detail.my_vote

  function vote(v: -1 | 1) {
    const next = previousVote === v ? 0 : v
    void act('vote', () => voteMarking(id, next), {
      message: next === 0 ? '已撤回你的投票' : next === 1 ? '已确认属实，谢谢' : '已记下你的异议',
      undo: async () => {
        const m = await voteMarking(id, previousVote)
        onChanged(m)
        await load()
      },
    })
  }

  async function addPhotos(files: FileList | null) {
    if (!files || files.length === 0) return
    const list = Array.from(files).slice(0, config.photo.max_per_uploader)
    setError(null)
    let added = 0
    for (let i = 0; i < list.length; i++) {
      const label = `上传 ${i + 1} / ${list.length}`
      setUpload({ label, progress: 0 })
      try {
        const photo = await preparePhoto(list[i], config.photo.max_bytes)
        try {
          await uploadMarkingPhoto(id, photo.blob, (r) => setUpload({ label, progress: r }))
          added += 1
        } finally {
          URL.revokeObjectURL(photo.url)
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err))
        break
      }
    }
    setUpload(null)
    if (fileRef.current) fileRef.current.value = ''
    const d = await load()
    if (d) onChanged(d)
    if (added) onToast({ message: `已补充 ${added} 张照片` })
  }

  return (
    <div className="mk-detail" style={{ '--mk-accent': meta.color } as CSSProperties}>
      <div className="mk-detail-nav">
        <button type="button" className="mk-back" onClick={onBack}>
          <Icon name="back" size={14} />
          返回列表
        </button>
        <button type="button" className="mk-link" onClick={() => onLocate(detail)}>
          <Icon name="pin" size={13} />
          在地图上看
        </button>
      </div>

      <header className="mk-detail-head">
        <TypeBadge type={detail.type} color={meta.color} soft={meta.soft} size={42} />
        <div>
          <p className="mk-kicker">
            {meta.label} · #{detail.id} · 第 {detail.version} 版
            {detail.mine && <span className="mk-mine">我提交的</span>}
          </p>
          <h3>{detail.title}</h3>
        </div>
        <StatusStamp status={detail.status} disputed={detail.disputed} />
      </header>

      {spec.note && <blockquote className="mk-note">{spec.note}</blockquote>}

      {detail.review && (
        <div className={`mk-review-box decision-${detail.review.decision}`}>
          <b>
            {
              {
                verify: '管理员已核实',
                reject: '管理员已驳回',
                archive: '已归档',
                reopen: '已重新打开',
              }[detail.review.decision]
            }
          </b>
          <span>
            {[...detail.review.basis, detail.review.reason].filter(Boolean).join('、')}
            {detail.review.note ? `：${detail.review.note}` : ''}
          </span>
          <small>{formatDateTime(detail.review.at)}</small>
        </div>
      )}

      {detail.status === 'pending' && !detail.mine && (
        <p className="mk-callout">
          这条标注还没有经过管理员核实：分析时只作为<b>建议</b>列出，你可以在报告里选择采纳。
        </p>
      )}
      {detail.status === 'pending' && detail.mine && (
        <p className="mk-callout mine">
          对你已经生效：你下次计算会计入它。其他人要等管理员核实后才会自动计入；补充照片能让核实更快。
        </p>
      )}

      <dl className="mk-facts">
        <div>
          <dt>提交</dt>
          <dd>{formatDate(detail.created_at)}</dd>
        </div>
        <div>
          <dt>有效期至</dt>
          <dd>{formatDate(detail.expires_at)}</dd>
        </div>
        <div>
          <dt>来源</dt>
          <dd>{config.sources[detail.source] ?? detail.source}</dd>
        </div>
        {detail.distance_m != null && (
          <div>
            <dt>距中心</dt>
            <dd>{formatDistance(detail.distance_m)}</dd>
          </div>
        )}
      </dl>

      <section className="mk-section">
        <h4>
          现场照片 <small>{detail.photos.length} 张</small>
          {active && detail.photos.length < config.photo.max_per_marking && (
            <label className="mk-link">
              <input
                ref={fileRef}
                type="file"
                hidden
                accept="image/jpeg,image/png,image/webp,image/heic,image/heif"
                multiple
                disabled={upload !== null}
                onChange={(e) => void addPhotos(e.target.files)}
              />
              <Icon name="camera" size={13} />
              {upload ? upload.label : detail.mine ? '补充照片' : '我也拍到了'}
            </label>
          )}
        </h4>
        {upload && (
          <div className="mk-progress inline" aria-hidden="true">
            <i style={{ width: `${Math.round(upload.progress * 100)}%` }} />
          </div>
        )}
        <PhotoGallery
          photos={detail.photos}
          emptyText={active ? '没有可见的现场照片，补一张才能被核实。' : '没有照片'}
          canDelete={(p) => (p.mine || canEdit) && !lastPhotoLocked}
          onDelete={async (p) => {
            try {
              await deleteMarkingPhoto(p.id, id)
              const d = await load()
              if (d) onChanged(d)
              onToast({ message: '照片已删除（文件同时从服务器移除）', tone: 'warn' })
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err))
            }
          }}
        />
      </section>

      {active && !detail.mine && (
        <section className="mk-section">
          <h4>你到过现场吗？</h4>
          <div className="mk-vote">
            <button
              type="button"
              className={previousVote === 1 ? 'on agree' : 'agree'}
              aria-pressed={previousVote === 1}
              disabled={busy !== null}
              onClick={() => vote(1)}
            >
              <Icon name="check" size={14} />
              属实 <b>{detail.confirms}</b>
            </button>
            <button
              type="button"
              className={previousVote === -1 ? 'on disagree' : 'disagree'}
              aria-pressed={previousVote === -1}
              disabled={busy !== null}
              onClick={() => vote(-1)}
            >
              <Icon name="alert" size={14} />
              有异议 <b>{detail.disputes}</b>
            </button>
          </div>
          <p className="mk-hint">投票帮助管理员判断先审哪条；标注是否生效只由审核决定。</p>
        </section>
      )}
      {detail.mine && (detail.confirms > 0 || detail.disputes > 0) && (
        <p className="mk-hint">
          {detail.confirms} 人确认属实，{detail.disputes} 人有异议。
        </p>
      )}

      {detail.mine && !token && (
        <p className="mk-callout warn">
          这台浏览器里没有这条标注的编辑凭据（可能清过浏览器数据），只能查看，不能修改或撤回。
        </p>
      )}

      {canEdit && editing && active && (
        <EditForm
          detail={detail}
          config={config}
          busy={busy !== null}
          onCancel={() => setEditing(false)}
          onSave={async (patch) => {
            const before = detail.version
            const m = await act('edit', () => updateMarking(id, before, patch))
            if (!m) return
            setEditing(false)
            if (m.version > before) {
              onToast({
                message:
                  detail.status === 'verified' ? '已保存。内容变了，需要重新核实' : '已保存为新版本',
                undo: async () => {
                  const back = await revertMarking(id, m.version, before)
                  onChanged(back)
                  await load()
                },
              })
            } else {
              onToast({ message: '有效期已更新' })
            }
          }}
        />
      )}

      {canEdit && !editing && (
        <div className="mk-actions">
          {active && (
            <button type="button" className="mk-btn" disabled={busy !== null} onClick={() => setEditing(true)}>
              <Icon name="edit" size={14} />
              修改
            </button>
          )}
          {active && (
            <button
              type="button"
              className="mk-btn danger"
              disabled={busy !== null}
              onClick={() =>
                void act('retract', () => retractMarking(id), {
                  message: '已撤回：分析时不再使用这条标注',
                  tone: 'warn',
                  undo: async () => {
                    const m = await restoreMarking(id)
                    onChanged(m)
                    await load()
                  },
                })
              }
            >
              <Icon name="undo" size={14} />
              撤回
            </button>
          )}
          {detail.status === 'retracted' && (
            <button
              type="button"
              className="mk-btn primary"
              disabled={busy !== null}
              onClick={() =>
                void act('restore', () => restoreMarking(id), {
                  message: '已恢复',
                  undo: async () => {
                    const m = await retractMarking(id)
                    onChanged(m)
                    await load()
                  },
                })
              }
            >
              恢复这条标注
            </button>
          )}
          {detail.status === 'expired' && (
            <button
              type="button"
              className="mk-btn primary"
              disabled={busy !== null}
              onClick={() =>
                void act('renew', () =>
                  updateMarking(id, detail.version, { expires_in_days: defaultDays(detail, config) }),
                )
              }
            >
              <Icon name="clock" size={14} />
              续期 {defaultDays(detail, config)} 天
            </button>
          )}
        </div>
      )}

      {detail.versions.length > 1 && (
        <details className="mk-section mk-fold">
          <summary>
            <Icon name="history" size={14} />
            历史版本 <small>{detail.versions.length}</small>
          </summary>
          <ol className="mk-versions">
            {[...detail.versions].reverse().map((ver) => (
              <li key={ver.version} className={ver.version === detail.version ? 'current' : ''}>
                <span>
                  <b>第 {ver.version} 版</b> {ver.title}
                </span>
                <small>{formatDateTime(ver.at)}</small>
                {canEdit && active && ver.version < detail.version && (
                  <button
                    type="button"
                    className="mk-link"
                    disabled={busy !== null}
                    onClick={() => {
                      const from = detail.version
                      void act('revert', () => revertMarking(id, from, ver.version), {
                        message: `已回到第 ${ver.version} 版的内容（保存为第 ${from + 1} 版）`,
                        undo: async () => {
                          const m = await revertMarking(id, from + 1, from)
                          onChanged(m)
                          await load()
                        },
                      })
                    }}
                  >
                    回到这一版
                  </button>
                )}
                {ver.version === detail.version && <em>当前</em>}
              </li>
            ))}
          </ol>
        </details>
      )}

      <details className="mk-section mk-fold">
        <summary>
          <Icon name="clock" size={14} />
          记录 <small>{detail.events.length}</small>
        </summary>
        <ol className="mk-timeline">
          {detail.events.map((e, i) => (
            <li key={`${e.at}-${i}`} className={`actor-${e.actor}`}>
              <i />
              <span>
                <b>{ACTOR_LABEL[e.actor] ?? e.actor}</b>
                {e.label}
                {e.detail && <em>{e.detail}</em>}
              </span>
              <small>{formatDateTime(e.at)}</small>
            </li>
          ))}
        </ol>
      </details>

      {error && (
        <p className="mk-error-line" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}

function defaultDays(m: Marking, config: MarkingConfig): number {
  if (m.type === 'closure') return config.closure_kinds[m.spec.kind ?? '']?.ttl_days ?? 60
  return config.type_ttl_days[m.type] ?? 180
}

function EditForm({
  detail,
  config,
  busy,
  onCancel,
  onSave,
}: {
  detail: Detail
  config: MarkingConfig
  busy: boolean
  onCancel: () => void
  onSave: (patch: MarkingPatch) => Promise<void>
}) {
  const spec = detail.spec
  const [note, setNote] = useState(spec.note ?? '')
  const [radius, setRadius] = useState(spec.radius_m ?? 50)
  const [kind, setKind] = useState(spec.kind ?? 'construction')
  const [name, setName] = useState(spec.name ?? '')
  const [reason, setReason] = useState(spec.reason ?? '')
  const [categories, setCategories] = useState<string[]>(spec.categories ?? [])
  const [renew, setRenew] = useState<number | null>(null)
  const [lo, hi] = config.limits.closure_radius_m

  const patch: MarkingPatch = {}
  if (note.trim() !== (spec.note ?? '')) patch.note = note.trim()
  if (detail.type === 'closure') {
    if (radius !== spec.radius_m) patch.radius_m = radius
    if (kind !== spec.kind) patch.kind = kind
  }
  if (detail.type === 'facility_extra' && name.trim() !== spec.name) patch.name = name.trim()
  if ((detail.type === 'facility_missing' || detail.type === 'gray_area') && reason !== spec.reason) {
    patch.reason = reason
  }
  if (
    detail.type === 'gray_area' &&
    categories.slice().sort().join() !== (spec.categories ?? []).slice().sort().join()
  ) {
    patch.categories = categories
  }
  if (renew !== null) patch.expires_in_days = renew
  const changed = Object.keys(patch).length > 0
  const reasons = detail.type === 'facility_missing' ? config.missing_reasons : config.gray_reasons

  return (
    <form
      className="mk-edit"
      onSubmit={(e) => {
        e.preventDefault()
        if (changed && !busy) void onSave(patch)
      }}
    >
      <h4>修改这条标注</h4>
      {detail.type === 'closure' && (
        <>
          <div className="mk-pills">
            {Object.entries(config.closure_kinds).map(([k, v]) => (
              <button
                key={k}
                type="button"
                className={kind === k ? 'on' : ''}
                aria-pressed={kind === k}
                onClick={() => setKind(k)}
              >
                {v.label}
              </button>
            ))}
          </div>
          <label className="mk-field">
            <span className="mk-label">
              半径 <b>{radius} 米</b>
            </span>
            <input
              type="range"
              min={lo}
              max={hi}
              step={5}
              value={radius}
              onChange={(e) => setRadius(Number(e.target.value))}
            />
          </label>
        </>
      )}
      {detail.type === 'facility_extra' && (
        <label className="mk-field">
          <span className="mk-label">设施名称</span>
          <input
            type="text"
            value={name}
            maxLength={config.limits.name_max}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
      )}
      {(detail.type === 'facility_missing' || detail.type === 'gray_area') && (
        <div className="mk-pills">
          {Object.entries(reasons).map(([k, label]) => (
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
      )}
      {detail.type === 'gray_area' && (
        <div className="mk-pills">
          {config.categories.map((c) => {
            const on = categories.includes(c)
            return (
              <button
                key={c}
                type="button"
                className={on ? 'on' : ''}
                aria-pressed={on}
                onClick={() => setCategories(on ? categories.filter((x) => x !== c) : [...categories, c])}
              >
                {c}
              </button>
            )
          })}
        </div>
      )}
      <label className="mk-field">
        <span className="mk-label">
          说明
          <small>
            {note.length} / {config.limits.note_max}
          </small>
        </span>
        <textarea rows={2} value={note} maxLength={config.limits.note_max} onChange={(e) => setNote(e.target.value)} />
      </label>
      <label className="mk-field mk-inline">
        <span className="mk-label">续期</span>
        <select value={renew ?? ''} onChange={(e) => setRenew(e.target.value ? Number(e.target.value) : null)}>
          <option value="">不改（至 {formatDate(detail.expires_at)}）</option>
          {[30, 60, 90, 180, 365].map((d) => (
            <option key={d} value={d}>
              从今天起 {d} 天（至 {daysFromNow(d)}）
            </option>
          ))}
        </select>
      </label>
      {detail.status === 'verified' && changed && Object.keys(patch).some((k) => k !== 'expires_in_days') && (
        <p className="mk-callout warn">改动内容后需要管理员重新核实；只续期不影响核实状态。</p>
      )}
      <div className="mk-actions">
        <button type="button" className="mk-btn" onClick={onCancel}>
          取消
        </button>
        <button type="submit" className="mk-btn primary" disabled={!changed || busy}>
          保存
        </button>
      </div>
    </form>
  )
}
