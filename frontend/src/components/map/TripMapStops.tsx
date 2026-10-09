import { useEffect, useRef, type RefObject } from 'react'
import type { TripMapData } from '../../types'
import { tripDrawerIsBottom, tripSegmentColor } from '../../lib/itinerary'

/** Project clickable station labels above buildings without moving their real coordinates. */
export function TripMapStops({ mapRef, ready, trip, interactive, onSelect }: {
  mapRef: RefObject<any>; ready: boolean; trip: TripMapData; interactive: boolean;
  onSelect?: (id: string | null) => void
}) {
  const root = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const map = mapRef.current, BMapGL = window.BMapGL
    if (!ready || !map || !BMapGL) return
    let frame = 0
    const positions = [trip.origin, ...trip.items]
    const paint = () => {
      const box = root.current?.getBoundingClientRect()
      const drawer = root.current?.closest('.stage')?.querySelector('.trip-drawer:not([hidden])')?.getBoundingClientRect()
      const edge = box ? drawer && !tripDrawerIsBottom(box, drawer) ? drawer.left - box.left - 12 : box.width - 8 : Infinity
      root.current?.querySelectorAll<HTMLElement>('[data-station]').forEach((label, i) => {
        const at = positions[i], pixel = map.pointToPixel(new BMapGL.Point(at.lng, at.lat))
        label.style.left = `${pixel.x}px`; label.style.top = `${pixel.y}px`
        label.classList.toggle('flipped', i > 0 && pixel.x - 14 + label.offsetWidth > edge && pixel.x > label.offsetWidth - 14)
        label.style.visibility = Number.isFinite(pixel.x) && Number.isFinite(pixel.y) ? 'visible' : 'hidden'
      })
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(() => { frame = 0; paint() }) }
    paint()
    const events = ['moving', 'moveend', 'zooming', 'zoomend', 'resize', 'tilt_changed', 'heading_changed']
    events.forEach(event => map.addEventListener(event, schedule))
    const observer = new ResizeObserver(schedule)
    if (root.current) observer.observe(root.current)
    return () => { cancelAnimationFrame(frame); observer.disconnect(); events.forEach(event => map.removeEventListener(event, schedule)) }
  }, [mapRef, ready, trip.origin, trip.items])
  return <div ref={root} className={`trip-map-stops${interactive ? '' : ' picking'}`}>
    <button type="button" data-station className="trip-map-origin" disabled={!interactive} onClick={() => onSelect?.(null)} aria-label="行程起点，查看整个行程"><b>起</b></button>
    {trip.items.map((item, i) => <button type="button" data-station key={item.entry_id}
      style={{ '--leg-color': tripSegmentColor(i) } as React.CSSProperties}
      className={`trip-map-stop${trip.selected === item.entry_id ? ' selected' : ''}${item.closure_status !== 'clear' ? ' uncertain' : ''}${item.closure_status === 'blocked' ? ' blocked' : ''}`}
      disabled={!interactive} aria-pressed={trip.selected === item.entry_id} aria-label={`第 ${i + 1} 站：${item.name}${item.gate ? `，${item.gate}` : ''}，查看对应路段`}
      title={`${item.name}${item.gate ? ` · ${item.gate}` : ''}`} onClick={() => onSelect?.(item.entry_id)}>
      <b>{i + 1}</b><span>{item.name}{item.gate ? ` · ${item.gate}` : ''}</span>{item.closure_status !== 'clear' && <small>{item.closure_status === 'blocked' ? '受阻' : '待确认'}</small>}
    </button>)}
  </div>
}
