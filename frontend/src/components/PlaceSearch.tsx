import { useEffect, useId, useRef, useState, type RefObject } from 'react'
import { searchPlaces, type SearchPlace } from '../api'

function placeAddress(place: SearchPlace) {
  let address = place.address
  for (const region of [place.city, place.district]) {
    if (region && address.startsWith(region)) address = address.slice(region.length).replace(/^[\s\-·,，]+/, '')
  }
  return [place.city, place.district, address].filter(Boolean).join(' · ') || '地址未提供'
}

export function PlaceSearch({ value, selected, inputRef, onChange, onChoose, disabled }: {
  value: string; selected: SearchPlace | null; inputRef: RefObject<HTMLInputElement | null>
  onChange: (value: string) => void; onChoose: (place: SearchPlace) => void; disabled: boolean
}) {
  const [city, setCity] = useState('上海市')
  const [response, setResponse] = useState<{ key: string; items: SearchPlace[]; message: string } | null>(null)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const sequence = useRef(0)
  const listId = useId()
  const key = JSON.stringify([city.trim(), value.trim()])
  const current = response?.key === key ? response : null
  const items = current?.items ?? []
  const message = value.trim().length < 2 ? '' : city.trim().length < 2
    ? '请输入城市，或填写“全国”'
    : current?.message ?? '正在查找地点…'
  useEffect(() => {
    const version = ++sequence.current
    if (!open || disabled || selected || value.trim().length < 2 || city.trim().length < 2) return
    const ctrl = new AbortController()
    const timer = setTimeout(() => {
      searchPlaces(value.trim(), city.trim(), ctrl.signal).then(results => {
        if (ctrl.signal.aborted || sequence.current !== version) return
        setResponse({ key, items: results, message: results.length ? '' : '没有找到候选，请补充地址或更换搜索城市。' })
      }).catch(err => {
        if (!ctrl.signal.aborted && sequence.current === version)
          setResponse({ key, items: [], message: err instanceof Error ? err.message : '搜索暂不可用，请稍后重试。' })
      })
    }, 400)
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [value, city, key, open, selected, disabled])
  function choose(place: SearchPlace) {
    sequence.current++; setOpen(false); setResponse(null); setActive(-1); onChoose(place)
  }
  return <div className="place-search-inputs" onBlur={e => {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setOpen(false)
  }}>
    <input ref={inputRef} role="combobox" aria-label="地址或小区名" aria-autocomplete="list"
      aria-controls={listId} aria-expanded={open && !selected}
      aria-activedescendant={open && !selected && items[active] ? `${listId}-${active}` : undefined}
      autoComplete="off" value={value} disabled={disabled} maxLength={120}
      placeholder="搜索地点，从候选中选择" onFocus={() => setOpen(true)}
      onChange={e => { setResponse(null); setActive(-1); onChange(e.target.value); setOpen(true) }}
      onKeyDown={e => {
        if (e.key === 'Escape') { setOpen(false); return }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault(); setOpen(true)
          if (items.length) setActive(i => i < 0 ? (e.key === 'ArrowDown' ? 0 : items.length - 1)
            : (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length)
        }
        if (e.key === 'Enter' && !selected) {
          e.preventDefault()
          if (active >= 0 && items[active]) choose(items[active])
          else setOpen(true)
        }
      }} />
    {open && !selected && <div className="place-candidates">
      <label className="search-city"><span>搜索城市</span><input aria-label="搜索城市（可填写全国）"
        value={city} maxLength={60} disabled={disabled} placeholder="城市名称，或填写全国" onChange={e => {
          setCity(e.target.value); setResponse(null); setActive(-1); onChange(value); setOpen(true)
        }} /></label>
      <div className="place-search-hint">搜索范围：{city || '请填写城市'} · 选择地点后再体检</div>
      <ul id={listId} role="listbox" aria-label="地点搜索候选">
        {items.map((place, i) => <li key={`${place.uid}-${place.lat}-${place.lng}`} role="option"
          id={`${listId}-${i}`} aria-selected={i === active}>
          <button type="button" onMouseDown={e => e.preventDefault()} onClick={() => choose(place)}>
            <strong>{place.name}</strong><span>{placeAddress(place)}</span>
          </button></li>)}
      </ul>
      {message && <p role="status">{message}</p>}
      {value.trim().length < 2 && <p>输入至少两个字查找地点。</p>}
    </div>}
    {selected && !disabled && <div className="place-selected" title={selected.address}>
      已选：{[selected.name, selected.city, selected.district].filter(Boolean).join(' · ')}
    </div>}
  </div>
}
