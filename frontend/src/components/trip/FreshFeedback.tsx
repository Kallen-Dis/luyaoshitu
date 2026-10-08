import { useEffect, useRef, useState } from 'react'
import { fetchFreshFeedback, submitFreshFeedback } from '../../api'
import type { FreshFeedbackSummary, Place, TripItem, TripPending } from '../../types'
import './feedback.css'

export function FreshFeedback({ place, disabled = false }: { place: Place | TripItem | TripPending; disabled?: boolean }) {
  const source = 'place' in place && place.place ? place.place : place
  const payload = { name: source.name, category: source.category, lat: source.lat, lng: source.lng }
  const key = JSON.stringify(payload)
  const [opened, setOpened] = useState(false)
  const [retry, setRetry] = useState(0)
  const [entry, setEntry] = useState<{ key: string; value: FreshFeedbackSummary } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyKey, setBusyKey] = useState<string | null>(null)
  const busy = busyKey === key
  const action = useRef<AbortController | null>(null)
  const current = useRef(key)
  useEffect(() => { current.current = key }, [key])
  const summary = entry?.key === key ? entry.value : null
  useEffect(() => {
    if (!opened || disabled) return
    const ctrl = new AbortController()
    void fetchFreshFeedback([JSON.parse(key)], ctrl.signal).then(result => {
      if (!ctrl.signal.aborted) { setEntry({ key, value: result.items[0] }); setError(null) }
    }).catch(err => { if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : '反馈读取失败') })
    return () => { ctrl.abort(); action.current?.abort() }
  }, [key, opened, retry, disabled])
  const vote = async (value: -1 | 0 | 1) => {
    if (busy || !summary || disabled) return
    const ctrl = new AbortController()
    action.current = ctrl
    setBusyKey(key); setError(null)
    try {
      const result = await submitFreshFeedback(payload, value, ctrl.signal)
      if (!ctrl.signal.aborted && current.current === key) setEntry({ key, value: result })
    } catch (err) { if (!ctrl.signal.aborted) setError(err instanceof Error ? err.message : '提交失败，请重试') }
    finally { setBusyKey(previous => previous === key ? null : previous) }
  }
  return <details className="fresh-feedback" onToggle={event => { if (event.currentTarget.open) setOpened(true) }}>
    <summary>售菜情况反馈{summary && <span> · {summary.confirms + summary.not_seen} 份近期反馈</span>}</summary>
    {disabled ? <p>模拟结果不提交门店反馈。</p> : <>
      <p className="fresh-feedback-stats" aria-live="polite">{summary ? `见到卖菜 ${summary.confirms} 份 · 未见卖菜 ${summary.not_seen} 份` : error ? '反馈暂时无法读取。' : '正在读取反馈…'}</p>
      <div className="fresh-feedback-actions">
        <button type="button" aria-pressed={summary?.my_feedback === 1} disabled={busy || !summary} onClick={() => void vote(summary?.my_feedback === 1 ? 0 : 1)}>见到在卖菜</button>
        <button type="button" aria-pressed={summary?.my_feedback === -1} disabled={busy || !summary} onClick={() => void vote(summary?.my_feedback === -1 ? 0 : -1)}>去过但未见卖菜</button>
        {summary && summary.my_feedback !== 0 && <button type="button" className="fresh-withdraw" disabled={busy} onClick={() => void vote(0)}>撤回反馈</button>}
      </div>
      {error && <p className="fresh-feedback-error" role="alert">{error}{!summary && <button type="button" onClick={() => setRetry(n => n + 1)}>重试</button>}</p>}
      <p>{summary?.my_feedback ? '已记录你的观察，可修改或撤回。' : '请根据到店观察反馈。'}统计近 {summary?.window_days ?? 90} 天，每台设备一份；未见卖菜可能只是临时缺货，反馈不等于已核实。</p>
      {summary?.latest_at && <small>最近反馈 {new Date(summary.latest_at).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' })}</small>}
    </>}
  </details>
}
