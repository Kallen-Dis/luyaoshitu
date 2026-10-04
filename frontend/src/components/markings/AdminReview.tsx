import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  fetchAdminMarking,
  fetchAdminPhoto,
  fetchAdminQueue,
  reviewMarking,
  setPhotoVisibility,
} from '../../api'
import {
  TYPE_META,
  adminToken,
  daysFromNow,
  formatDate,
  formatDateTime,
  saveAdminToken,
} from '../../lib/markings'
import type { AdminMarkingDetail, Marking, MarkingConfig, MarkingPhoto, ReviewInput } from '../../types'
import { Icon, TypeBadge } from './Icon'
import { StatusStamp } from './MarkingDetail'
import { PhotoGallery } from './PhotoGallery'

interface Props {
  config: MarkingConfig
  onClose: () => void
  onLocate: (marking: Marking) => void
  /** 审核改变了标注状态：地图与侧栏要刷新 */
  onChanged: () => void
}

const QUEUES: { key: string; label: string }[] = [
  { key: 'pending', label: '待核实' },
  { key: 'disputed', label: '有争议' },
  { key: 'expired', label: '已过期' },
  { key: 'verified', label: '已核实' },
  { key: 'rejected', label: '已驳回' },
  { key: 'archived', label: '已归档' },
]

const CHECKS: { key: 'location' | 'type' | 'current'; label: string; hint: string }[] = [
  { key: 'location', label: '位置准确', hint: '对照照片与底图，标在了正确的地方' },
  { key: 'type', label: '类型与描述相符', hint: '确实是围挡 / 设施失效 / 补录 / 缺设施' },
  { key: 'current', label: '目前仍然有效', hint: '不是早已拆除、早已恢复的旧情况' },
]

type Decision = ReviewInput['decision']

export function AdminReview({ config, onClose, onLocate, onChanged }: Props) {
  const [token, setToken] = useState(adminToken() ?? '')
  const [authed, setAuthed] = useState(false)
  const [queue, setQueue] = useState('pending')
  const [items, setItems] = useState<Marking[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  // 本次会话已存过口令时，打开面板就直接去拉队列
  const [loading, setLoading] = useState(() => Boolean(adminToken()))
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !document.querySelector('.mk-lightbox, .mk-confirm')) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const receive = useCallback((pending: Promise<Awaited<ReturnType<typeof fetchAdminQueue>>>) => {
    return pending
      .then((data) => {
        setItems(data.items)
        setCounts(data.counts)
        setAuthed(true)
        setError(null)
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && (err.httpStatus === 401 || err.httpStatus === 429)) {
          saveAdminToken(null)
          setAuthed(false)
        }
        setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => setLoading(false))
  }, [])

  const loadQueue = useCallback(
    (which: string) => {
      setLoading(true)
      return receive(fetchAdminQueue(which))
    },
    [receive],
  )

  // 已存过口令（本次会话）就直接进队列
  useEffect(() => {
    if (adminToken()) void receive(fetchAdminQueue('pending'))
  }, [receive])

  return (
    <aside className="mk-drawer" role="dialog" aria-modal="false" aria-label="标注审核">
      <header className="mk-drawer-head">
        <span className="mk-drawer-mark">
          <Icon name="shield" size={18} />
        </span>
        <div>
          <p className="mk-kicker">管理员</p>
          <h2>标注审核</h2>
        </div>
        <button ref={closeRef} type="button" className="mk-icon-btn" aria-label="关闭审核面板" onClick={onClose}>
          <Icon name="close" />
        </button>
      </header>

      {!authed ? (
        <form
          className="mk-gate"
          onSubmit={(e) => {
            e.preventDefault()
            if (!token.trim()) return
            saveAdminToken(token.trim())
            void loadQueue(queue)
          }}
        >
          <p>输入管理员口令（.env 里的 ADMIN_TOKEN）。口令只保存在这个浏览器标签页里，关闭即失效。</p>
          <input
            type="password"
            autoComplete="current-password"
            value={token}
            placeholder="管理员口令"
            aria-label="管理员口令"
            onChange={(e) => setToken(e.target.value)}
          />
          <button type="submit" className="mk-btn primary" disabled={!token.trim() || loading}>
            {loading ? '验证中…' : '进入审核'}
          </button>
          {error && <p className="mk-error-line">{error}</p>}
        </form>
      ) : selected !== null ? (
        <ReviewDetail
          key={selected}
          id={selected}
          config={config}
          onBack={() => {
            setSelected(null)
            void loadQueue(queue)
          }}
          onLocate={onLocate}
          onReviewed={() => {
            onChanged()
            void loadQueue(queue)
          }}
        />
      ) : (
        <>
          <nav className="mk-queue-tabs" aria-label="审核队列">
            {QUEUES.map((q) => (
              <button
                key={q.key}
                type="button"
                className={queue === q.key ? 'on' : ''}
                aria-pressed={queue === q.key}
                onClick={() => {
                  setQueue(q.key)
                  void loadQueue(q.key)
                }}
              >
                {q.label}
                <small>{counts[q.key] ?? 0}</small>
              </button>
            ))}
          </nav>
          <div className="mk-drawer-body">
            {error && <p className="mk-error-line">{error}</p>}
            {loading && items.length === 0 ? (
              <div className="mk-skeleton" />
            ) : items.length === 0 ? (
              <div className="mk-empty">
                <Icon name="check" size={22} />
                <p>这个队列是空的。</p>
              </div>
            ) : (
              <ul className="mk-list">
                {items.map((m) => {
                  const meta = TYPE_META[m.type]
                  return (
                    <li key={m.id}>
                      <button type="button" className="mk-row" onClick={() => setSelected(m.id)}>
                        <TypeBadge type={m.type} color={meta.color} soft={meta.soft} />
                        <span className="mk-row-body">
                          <b>{m.title}</b>
                          <span className="mk-row-meta">
                            #{m.id} · {formatDate(m.created_at)}
                            <span>
                              <Icon name="camera" size={12} />
                              {m.photo_count}
                            </span>
                            <span>
                              <Icon name="check" size={12} />
                              {m.confirms}
                              <Icon name="alert" size={12} />
                              {m.disputes}
                            </span>
                          </span>
                        </span>
                        <StatusStamp status={m.status} disputed={m.disputed} />
                      </button>
                    </li>
                  )
                })}
              </ul>
            )}
          </div>
          <footer className="mk-drawer-foot">
            <button
              type="button"
              className="mk-link"
              onClick={() => {
                saveAdminToken(null)
                setAuthed(false)
                setToken('')
              }}
            >
              退出管理员
            </button>
          </footer>
        </>
      )}
    </aside>
  )
}

function ReviewDetail({
  id,
  config,
  onBack,
  onLocate,
  onReviewed,
}: {
  id: number
  config: MarkingConfig
  onBack: () => void
  onLocate: (marking: Marking) => void
  onReviewed: () => void
}) {
  const [detail, setDetail] = useState<AdminMarkingDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [openedAt, setOpenedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())
  const [srcs, setSrcs] = useState<Record<string, string>>({})
  const [viewed, setViewed] = useState<Set<string>>(() => new Set())
  const [decision, setDecision] = useState<Decision>('verify')
  const [checks, setChecks] = useState({ location: false, type: false, current: false })
  const [basis, setBasis] = useState<string[]>([])
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('')
  const [renew, setRenew] = useState<number | null>(null)
  const [confirm, setConfirm] = useState<ReviewInput | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [hiding, setHiding] = useState<{ photo: MarkingPhoto; note: string } | null>(null)
  // 已取到的照片 blob 地址：卸载时统一释放。只在副作用与事件里读写，不参与渲染
  const srcsRef = useRef<Record<string, string>>({})

  const apply = useCallback(async (d: AdminMarkingDetail, isCancelled: () => boolean) => {
    setDetail(d)
    // 服务端在这一刻记下「已打开」；前端的计时只是把服务端规则提前显示出来
    setOpenedAt(Date.now())
    setError(null)
    const entries = await Promise.all(
      d.photos
        .filter((p) => !srcsRef.current[p.id])
        .map(async (p) => {
          try {
            return [p.id, await fetchAdminPhoto(p.id)] as const
          } catch {
            return null
          }
        }),
    )
    for (const e of entries) {
      if (!e) continue
      if (isCancelled()) URL.revokeObjectURL(e[1])
      else srcsRef.current[e[0]] = e[1]
    }
    if (!isCancelled()) setSrcs({ ...srcsRef.current })
  }, [])

  const load = useCallback(async () => {
    try {
      await apply(await fetchAdminMarking(id), () => false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [apply, id])

  useEffect(() => {
    let cancelled = false
    fetchAdminMarking(id)
      .then((d) => (cancelled ? undefined : apply(d, () => cancelled)))
      .catch((err: Error) => {
        if (!cancelled) setError(err.message)
      })
    return () => {
      cancelled = true
    }
  }, [apply, id])

  useEffect(() => {
    const held = srcsRef.current
    return () => Object.values(held).forEach((u) => URL.revokeObjectURL(u))
  }, [])

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 500)
    return () => window.clearInterval(t)
  }, [])

  const visiblePhotos = useMemo(
    () => (detail?.photos ?? []).filter((p) => p.status === 'visible'),
    [detail],
  )

  if (!detail) {
    return (
      <div className="mk-drawer-body">
        <button type="button" className="mk-back" onClick={onBack}>
          <Icon name="back" size={14} />
          返回队列
        </button>
        {error ? <p className="mk-error-line">{error}</p> : <div className="mk-skeleton" />}
      </div>
    )
  }

  const meta = TYPE_META[detail.type]
  const elapsed = openedAt ? (now - openedAt) / 1000 : 0
  const minSeconds = detail.review_ticket.min_seconds
  const waitLeft = Math.max(0, Math.ceil(minSeconds - elapsed))
  const unseen = visiblePhotos.filter((p) => !viewed.has(p.id)).length
  const expired = detail.status === 'expired'
  const canVerify = detail.status === 'pending' || expired
  const noteOk = note.trim().length >= config.review.note_min

  const verifyMissing: string[] = []
  if (detail.self_submitted) verifyMissing.push('不能核实自己这台设备提交的标注')
  if (!CHECKS.every((c) => checks[c.key])) verifyMissing.push('逐项确认三项检查')
  if (basis.length === 0) verifyMissing.push('选择核实依据')
  if (basis.includes('photo') && unseen > 0) verifyMissing.push(`还有 ${unseen} 张照片没看`)
  if (!noteOk) verifyMissing.push(`审核意见至少 ${config.review.note_min} 个字`)
  if (expired && renew === null) verifyMissing.push('重新设定有效期')
  if (waitLeft > 0) verifyMissing.push(`再看 ${waitLeft} 秒`)

  const otherMissing: string[] = []
  if (!noteOk) otherMissing.push(`审核意见至少 ${config.review.note_min} 个字`)
  if (decision === 'reject' && !reason) otherMissing.push('选择驳回原因')

  const available: Decision[] = canVerify
    ? ['verify', 'reject', 'archive']
    : detail.status === 'verified'
      ? ['reject', 'archive']
      : detail.status === 'rejected' || detail.status === 'archived'
        ? ['reopen']
        : []
  const current = available.includes(decision) ? decision : available[0]
  const missing = current === 'verify' ? verifyMissing : otherMissing

  function prepare() {
    if (!detail || !current || missing.length > 0) return
    const input: ReviewInput = { decision: current, version: detail.version, note: note.trim() }
    if (current === 'verify') {
      input.basis = basis
      input.checks = checks
      input.photos_reviewed = [...viewed]
      if (renew !== null) input.expires_in_days = renew
    }
    if (current === 'reject') input.reason = reason
    setConfirm(input)
  }

  async function submit() {
    if (!confirm) return
    setSubmitting(true)
    setError(null)
    try {
      const d = await reviewMarking(id, confirm)
      setDetail(d)
      setOpenedAt(Date.now())
      setConfirm(null)
      onReviewed()
    } catch (err) {
      setConfirm(null)
      setError(err instanceof Error ? err.message : String(err))
      if (err instanceof ApiError && ['version_conflict', 'review_stale'].includes(err.code)) {
        await load()
      }
    } finally {
      setSubmitting(false)
    }
  }

  const DECISION_LABEL: Record<Decision, string> = {
    verify: '核实',
    reject: '驳回',
    archive: '归档',
    reopen: '重新打开',
  }

  return (
    <div className="mk-drawer-body">
      <div className="mk-detail-nav">
        <button type="button" className="mk-back" onClick={onBack}>
          <Icon name="back" size={14} />
          返回队列
        </button>
        <button type="button" className="mk-link" onClick={() => onLocate(detail)}>
          <Icon name="pin" size={13} />
          在地图上定位
        </button>
      </div>

      <header className="mk-detail-head">
        <TypeBadge type={detail.type} color={meta.color} soft={meta.soft} size={42} />
        <div>
          <p className="mk-kicker">
            {meta.label} · #{detail.id} · 第 {detail.version} 版 · {config.sources[detail.source]}
          </p>
          <h3>{detail.title}</h3>
        </div>
        <StatusStamp status={detail.status} disputed={detail.disputed} />
      </header>
      {detail.spec.note && <blockquote className="mk-note">{detail.spec.note}</blockquote>}

      <dl className="mk-facts">
        <div>
          <dt>提交</dt>
          <dd>{formatDateTime(detail.created_at)}</dd>
        </div>
        <div>
          <dt>有效期至</dt>
          <dd>{formatDate(detail.expires_at)}</dd>
        </div>
        <div>
          <dt>投票</dt>
          <dd>
            属实 {detail.confirms} · 异议 {detail.disputes}
          </dd>
        </div>
        <div>
          <dt>提交设备</dt>
          <dd>
            共 {detail.author_history.total} 条 · 已核实 {detail.author_history.verified} · 驳回{' '}
            {detail.author_history.rejected}
          </dd>
        </div>
      </dl>

      <section className="mk-section">
        <h4>
          现场照片 <small>{visiblePhotos.length} 张可见</small>
          {visiblePhotos.length > 0 && (
            <span className={unseen === 0 ? 'mk-seen ok' : 'mk-seen'}>
              已查看 {visiblePhotos.length - unseen} / {visiblePhotos.length}
            </span>
          )}
        </h4>
        <PhotoGallery
          photos={detail.photos}
          srcOf={(p) => srcs[p.id]}
          viewed={viewed}
          onView={(p) => setViewed((prev) => (prev.has(p.id) ? prev : new Set(prev).add(p.id)))}
          emptyText="没有照片：依据不能选「现场照片」"
        />
        {detail.photos.length > 0 && (
          <details className="mk-fold">
            <summary>照片管理</summary>
            <ul className="mk-photo-admin">
              {detail.photos.map((p, i) => (
                <li key={p.id}>
                  <span>
                    第 {i + 1} 张 · {p.role === 'author' ? '提交者' : '补充'} · {formatDateTime(p.created_at)}
                  </span>
                  <button
                    type="button"
                    className="mk-link"
                    onClick={() => setHiding({ photo: p, note: '' })}
                  >
                    <Icon name={p.status === 'hidden' ? 'eye' : 'eye-off'} size={13} />
                    {p.status === 'hidden' ? '恢复显示' : '隐藏'}
                  </button>
                </li>
              ))}
            </ul>
            {hiding && (
              <form
                className="mk-inline-form"
                onSubmit={async (e) => {
                  e.preventDefault()
                  try {
                    await setPhotoVisibility(hiding.photo.id, hiding.photo.status === 'visible', hiding.note)
                    setHiding(null)
                    await load()
                  } catch (err) {
                    setError(err instanceof Error ? err.message : String(err))
                  }
                }}
              >
                <input
                  type="text"
                  value={hiding.note}
                  maxLength={100}
                  placeholder={hiding.photo.status === 'visible' ? '隐藏原因，如：拍到了行人正脸' : '恢复原因'}
                  onChange={(e) => setHiding({ ...hiding, note: e.target.value })}
                />
                <button type="submit" className="mk-btn small" disabled={!hiding.note.trim()}>
                  确定
                </button>
                <button type="button" className="mk-btn small" onClick={() => setHiding(null)}>
                  取消
                </button>
              </form>
            )}
          </details>
        )}
      </section>

      {detail.versions.length > 1 && (
        <details className="mk-section mk-fold">
          <summary>
            <Icon name="history" size={14} />
            版本 <small>{detail.versions.length}</small>
          </summary>
          <ol className="mk-versions">
            {[...detail.versions].reverse().map((v) => (
              <li key={v.version} className={v.version === detail.version ? 'current' : ''}>
                <span>
                  <b>第 {v.version} 版</b> {v.title}
                  {v.spec?.note ? ` · ${v.spec.note}` : ''}
                </span>
                <small>{formatDateTime(v.at)}</small>
              </li>
            ))}
          </ol>
        </details>
      )}

      <details className="mk-section mk-fold">
        <summary>
          <Icon name="clock" size={14} />
          完整记录 <small>{detail.events.length}</small>
        </summary>
        <ol className="mk-timeline">
          {detail.events.map((e, i) => (
            <li key={`${e.at}-${i}`} className={`actor-${e.actor}`}>
              <i />
              <span>
                <b>{{ author: '提交者', other: '其他用户', admin: '管理员', system: '系统' }[e.actor]}</b>
                {e.label}
                {e.detail && <em>{e.detail}</em>}
              </span>
              <small>{formatDateTime(e.at)}</small>
            </li>
          ))}
        </ol>
      </details>

      {available.length > 0 && current && (
        <section className="mk-review-form" aria-label="审核">
          <div className="mk-segmented" role="radiogroup" aria-label="审核决定">
            {available.map((d) => (
              <button
                key={d}
                type="button"
                role="radio"
                aria-checked={current === d}
                className={current === d ? `on d-${d}` : ''}
                onClick={() => setDecision(d)}
              >
                {DECISION_LABEL[d]}
              </button>
            ))}
          </div>

          {current === 'verify' && (
            <>
              {detail.self_submitted && (
                <p className="mk-callout warn">这是你这台设备提交的标注，需要由另一台管理员设备核实。</p>
              )}
              <fieldset className="mk-checks">
                <legend className="mk-label">逐项确认</legend>
                {CHECKS.map((c) => (
                  <label key={c.key}>
                    <input
                      type="checkbox"
                      checked={checks[c.key]}
                      onChange={(e) => setChecks({ ...checks, [c.key]: e.target.checked })}
                    />
                    <span>
                      <b>{c.label}</b>
                      <em>{c.hint}</em>
                    </span>
                  </label>
                ))}
              </fieldset>
              <fieldset className="mk-field">
                <legend className="mk-label">核实依据</legend>
                <div className="mk-pills">
                  {Object.entries(config.review_basis).map(([k, label]) => {
                    const disabled =
                      (k === 'photo' && visiblePhotos.length === 0) ||
                      (k === 'multi_confirm' && detail.confirms < 2)
                    const on = basis.includes(k)
                    return (
                      <button
                        key={k}
                        type="button"
                        className={on ? 'on' : ''}
                        aria-pressed={on}
                        disabled={disabled}
                        title={
                          k === 'photo' && disabled
                            ? '没有照片'
                            : k === 'multi_confirm' && disabled
                              ? '确认人数不到 2 人'
                              : undefined
                        }
                        onClick={() => setBasis(on ? basis.filter((b) => b !== k) : [...basis, k])}
                      >
                        {label}
                      </button>
                    )
                  })}
                </div>
              </fieldset>
              {expired && (
                <label className="mk-field mk-inline">
                  <span className="mk-label">新的有效期</span>
                  <select value={renew ?? ''} onChange={(e) => setRenew(e.target.value ? Number(e.target.value) : null)}>
                    <option value="">请选择</option>
                    {[30, 60, 90, 180, 365].map((d) => (
                      <option key={d} value={d}>
                        {d} 天（至 {daysFromNow(d)}）
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </>
          )}

          {current === 'reject' && (
            <fieldset className="mk-field">
              <legend className="mk-label">驳回原因</legend>
              <div className="mk-pills">
                {Object.entries(config.reject_reasons).map(([k, label]) => (
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
          )}

          <label className="mk-field">
            <span className="mk-label">
              审核意见
              <small>
                {note.trim().length} / 至少 {config.review.note_min} 字
              </small>
            </span>
            <textarea
              rows={3}
              value={note}
              maxLength={200}
              placeholder={
                current === 'verify'
                  ? '说明你依据什么做的判断，如：照片清楚，现场确有围挡，封闭了人行道'
                  : '说明原因，提交者与其他用户都能看到'
              }
              onChange={(e) => setNote(e.target.value)}
            />
          </label>

          {missing.length > 0 && (
            <ul className="mk-missing" aria-label="还差的条件">
              {missing.map((m) => (
                <li key={m}>{m}</li>
              ))}
            </ul>
          )}
          <button
            type="button"
            className={`mk-btn primary wide d-${current}`}
            disabled={missing.length > 0}
            onClick={prepare}
          >
            {current === 'verify' && waitLeft > 0 ? `请先看完详情（${waitLeft}）` : `${DECISION_LABEL[current]}…`}
          </button>
        </section>
      )}

      {error && (
        <p className="mk-error-line" role="alert">
          {error}
        </p>
      )}

      {confirm && (
        <div className="mk-confirm" role="alertdialog" aria-modal="true" aria-label="确认审核">
          <div className="mk-confirm-card">
            <h3>确认{DECISION_LABEL[confirm.decision]}这条标注？</h3>
            <p className="mk-confirm-title">
              #{detail.id}「{detail.title}」
            </p>
            <dl>
              {confirm.basis && (
                <>
                  <dt>依据</dt>
                  <dd>{confirm.basis.map((b) => config.review_basis[b]).join('、')}</dd>
                </>
              )}
              {confirm.reason && (
                <>
                  <dt>原因</dt>
                  <dd>{config.reject_reasons[confirm.reason]}</dd>
                </>
              )}
              <dt>意见</dt>
              <dd>{confirm.note}</dd>
            </dl>
            <p className="mk-confirm-effect">
              {confirm.decision === 'verify'
                ? '核实后，附近所有用户分析时都会自动计入这条标注，审核意见公开显示。'
                : confirm.decision === 'reject'
                  ? '驳回后，这条标注不再对其他用户显示，也不参与任何计算。'
                  : confirm.decision === 'archive'
                    ? '归档后，这条标注不再参与计算，记录保留。'
                    : '重新打开后回到待核实，分析时只作为建议。'}
            </p>
            <div className="mk-actions">
              <button type="button" className="mk-btn" disabled={submitting} onClick={() => setConfirm(null)}>
                返回修改
              </button>
              <button
                type="button"
                className={`mk-btn primary d-${confirm.decision}`}
                disabled={submitting}
                onClick={() => void submit()}
                autoFocus
              >
                {submitting ? '提交中…' : `确认${DECISION_LABEL[confirm.decision]}`}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
