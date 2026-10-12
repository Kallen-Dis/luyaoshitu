import { useId, useState, type AriaRole, type ReactNode } from 'react'

interface Props {
  title: string
  children: ReactNode
  defaultExpanded?: boolean
  className?: string
  ariaLabel?: string
  role?: AriaRole
  headerActions?: ReactNode
}

/** 保留标题和编号；收起内容时不卸载内部组件，避免丢失当前操作状态。 */
export function ReportSection({
  title,
  children,
  defaultExpanded = true,
  className,
  ariaLabel,
  role,
  headerActions,
}: Props) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const bodyId = useId()

  return (
    <section className={['card', 'report-section', className].filter(Boolean).join(' ')} aria-label={ariaLabel ?? title} role={role}>
      <div className="report-section-head">
        <h2 className="card-title">{title}</h2>
        <div className="report-section-actions">
          {headerActions}
          <button
            type="button"
            className="report-section-toggle"
            aria-label={`${expanded ? '收起' : '展开'}${title}`}
            aria-expanded={expanded}
            aria-controls={bodyId}
            onClick={() => setExpanded(value => !value)}
          >
            {expanded ? '收起' : '展开'}
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
              <path d={expanded ? 'm4 10 4-4 4 4' : 'm4 6 4 4 4-4'} />
            </svg>
          </button>
        </div>
      </div>
      <div id={bodyId} className="report-section-body" hidden={!expanded}>
        {children}
      </div>
    </section>
  )
}
