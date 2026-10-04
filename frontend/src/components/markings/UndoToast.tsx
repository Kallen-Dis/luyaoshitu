import { useState } from 'react'
import { Icon } from './Icon'

export interface ToastSpec {
  /** 每条提示一个新 id：同样的文字连着出现两次也会重新计时 */
  id: number
  message: string
  tone?: 'ok' | 'warn'
  undoLabel?: string
  undo?: () => Promise<void>
}

/**
 * 底部提示条，10 秒后自动消失。倒计时由 CSS 动画驱动：鼠标移上去动画暂停，
 * 消失时刻跟着顺延，用户读完再决定要不要撤销。
 */
export function UndoToast({ toast, onDone }: { toast: ToastSpec; onDone: () => void }) {
  // 调用方以 toast.id 作 key 渲染，换一条提示就是一个新实例，busy / error 不会串
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function undo() {
    if (!toast.undo || busy) return
    setBusy(true)
    setError(null)
    try {
      await toast.undo()
      onDone()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className={`mk-toast tone-${toast.tone ?? 'ok'}`}
      role="status"
      aria-live="polite"
    >
      <span className="mk-toast-icon">
        <Icon name={toast.tone === 'warn' ? 'alert' : 'check'} size={16} />
      </span>
      <span className="mk-toast-text">
        {toast.message}
        {error && <em>撤销失败：{error}</em>}
      </span>
      {toast.undo && (
        <button type="button" className="mk-toast-undo" disabled={busy} onClick={() => void undo()}>
          <Icon name="undo" size={14} />
          {busy ? '撤销中…' : (toast.undoLabel ?? '撤销')}
        </button>
      )}
      <button type="button" className="mk-toast-close" aria-label="关闭提示" onClick={onDone}>
        <Icon name="close" size={14} />
      </button>
      <span className="mk-toast-timer" onAnimationEnd={() => !busy && onDone()} />
    </div>
  )
}
