import { useEffect, useState, type ReactNode } from 'react'
import { scheduleNoticeDismiss } from '../../lib/notice'

/** key 由提示事件决定，连续的 GPS 更新不延长普通提示的展示时间。 */
export function GuideNotice({ children, className, sticky = false }: { children: ReactNode; className: string; sticky?: boolean }) {
  const [dismissed, setDismissed] = useState(false)
  useEffect(() => {
    if (!sticky) return scheduleNoticeDismiss(() => setDismissed(true))
  }, [sticky])
  if (dismissed && !sticky) return null
  return <div className={`${className} guide-notice${sticky ? ' persistent' : ''}`} role="status">
    <div>{children}</div>
    {!sticky && <button type="button" className="guide-notice-close" aria-label="收起提示" onClick={() => setDismissed(true)}><svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m6 6 8 8M14 6l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg></button>}
  </div>
}
