import { PLACE_MARK, SHORT_NAME } from '../map/context'

export function TripCategoryPicker({ value, failed, disabled, onChange }: {
  value: string | null; failed: string[]; disabled?: boolean; onChange: (category: string) => void
}) {
  const categories = Object.keys(PLACE_MARK)
  const enabled = categories.filter(c => !failed.includes(c))
  return <div className="trip-categories" role="radiogroup" aria-label="选择设施品类">
    {categories.map(category => <button type="button" role="radio" key={category}
      aria-checked={category === value} disabled={disabled || failed.includes(category)}
      tabIndex={category === value || (!value && enabled[0] === category) ? 0 : -1}
      title={failed.includes(category) ? '这一类检索失败，数量未知' : SHORT_NAME[category]}
      style={{ '--trip-color': PLACE_MARK[category].color } as React.CSSProperties}
      onClick={() => onChange(category)} onKeyDown={event => {
        if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return
        event.preventDefault()
        const step = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1
        const next = enabled[(enabled.indexOf(category) + step + enabled.length) % enabled.length]
        onChange(next)
        const group = event.currentTarget.parentElement
        const button = Array.from(group?.querySelectorAll<HTMLButtonElement>('button') ?? []).find(b => b.title === SHORT_NAME[next])
        button?.focus()
      }}><span>{PLACE_MARK[category].glyph}</span><small>{SHORT_NAME[category]}</small></button>)}
  </div>
}
