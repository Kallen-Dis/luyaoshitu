import { useEffect, useRef, type ReactNode } from 'react'
import '../trip/trip.css'
import './planning.css'

export function PlanningDrawer({ mode, busy, onMode, onClose, children, footer }: {
  mode: 'facility' | 'closure'; busy: boolean
  onMode: (mode: 'facility' | 'closure') => void; onClose: () => void
  children: ReactNode; footer: ReactNode
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    heading.current?.focus()
    return () => trigger?.focus()
  }, [])
  return <section className="trip-drawer planning-drawer floating" role="dialog" aria-labelledby="planning-title" aria-modal="false" aria-busy={busy}>
    <header className="trip-head"><div><h2 id="planning-title" ref={heading} tabIndex={-1}>规划模拟</h2>
      <p>{mode === 'facility' ? '比较增建后的设施覆盖与评分' : '比较封闭后的可达范围与评分'}</p></div>
      <button type="button" className="trip-close" aria-label="关闭规划模拟" onClick={onClose}>×</button>
    </header>
    <div className="trip-scroll">
      <div className="trip-modes" role="group" aria-label="模拟方案">
        <button type="button" aria-pressed={mode === 'facility'} onClick={() => { if (mode !== 'facility') onMode('facility') }}>增建设施</button>
        <button type="button" aria-pressed={mode === 'closure'} onClick={() => { if (mode !== 'closure') onMode('closure') }}>道路封闭</button>
      </div>
      {children}
    </div>
    <footer className="trip-footer planning-footer"><p>假设方案 · 实际设施或封路可通过共享标注补充</p>{footer}</footer>
  </section>
}
