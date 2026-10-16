import { useEffect, useRef, type RefObject } from 'react'
import { routeArrowPositions } from '../../lib/itinerary'

export interface MapOutline {
  rings: readonly (readonly (readonly [number, number])[])[]
  weight: number
  color: string
  alpha?: number
  dash?: number[]
  haloWidth?: number
  anchor?: { lat: number; lng: number }
  closed?: boolean
  arrows?: boolean
  /** 给出颜色时，环内（奇偶规则）铺斜线 */
  hatch?: string
  dots?: string
  label?: string
}

const hatchPatterns = new Map<string, HTMLCanvasElement>()

/** 8×8 的斜线小图，平铺成「确认走不到」的阴影线。按屏幕像素铺，不随缩放变疏变密。 */
function hatchTile(color: string): HTMLCanvasElement {
  const cached = hatchPatterns.get(color)
  if (cached) return cached
  const tile = document.createElement('canvas')
  tile.width = 8
  tile.height = 8
  const c = tile.getContext('2d')
  if (c) {
    c.strokeStyle = color
    c.lineWidth = 1.4
    c.beginPath()
    c.moveTo(-2, 10)
    c.lineTo(10, -2)
    c.moveTo(-2, 2)
    c.lineTo(2, -2)
    c.moveTo(6, 10)
    c.lineTo(10, 6)
    c.stroke()
  }
  hatchPatterns.set(color, tile)
  return tile
}

function dotsTile(color: string): HTMLCanvasElement {
  const key = `dots-${color}`
  const cached = hatchPatterns.get(key)
  if (cached) return cached
  const tile = document.createElement('canvas')
  tile.width = tile.height = 10
  const ctx = tile.getContext('2d')
  if (ctx) {
    ctx.fillStyle = color
    ctx.beginPath()
    ctx.arc(5, 5, 1.1, 0, Math.PI * 2)
    ctx.fill()
  }
  hatchPatterns.set(key, tile)
  return tile
}

interface Props {
  mapRef: RefObject<any>
  ready: boolean
  active: boolean
  outlines: readonly MapOutline[]
  opacity: number
  arrowsOnly?: boolean
}

/** 平面视图的圈线与区域外框单独叠在建筑之上；填充与点击仍由原地图覆盖物负责。 */
export function MapOutlines({ mapRef, ready, active, outlines, opacity, arrowsOnly = false }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!canvas || !map || !BMapGL || !ready || !active) return
    let frame = 0

    const paint = () => {
      const width = canvas.clientWidth
      const height = canvas.clientHeight
      const dpr = window.devicePixelRatio || 1
      if (canvas.width !== Math.round(width * dpr)) canvas.width = Math.round(width * dpr)
      if (canvas.height !== Math.round(height * dpr)) canvas.height = Math.round(height * dpr)
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx.clearRect(0, 0, width, height)
      ctx.lineJoin = 'round'
      ctx.lineCap = 'round'
      ctx.miterLimit = 2
      const paths = outlines.map(outline => {
        const path = new Path2D()
        const arrows = []
        for (const ring of outline.rings) {
          const pixels = ring.map(([lng, lat]) => map.pointToPixel(new BMapGL.Point(lng, lat)))
          pixels.forEach((p: { x: number; y: number }, index: number) => {
            if (index === 0) path.moveTo(p.x, p.y)
            else path.lineTo(p.x, p.y)
          })
          if (outline.closed !== false) path.closePath()
          if (outline.arrows) arrows.push(...routeArrowPositions(pixels))
        }
        const labelPoint = outline.label ? (outline.rings[0] ?? [])
          .map(([lng, lat]) => map.pointToPixel(new BMapGL.Point(lng, lat)))
          .filter((p: { x: number; y: number }) => p.x > 70 && p.x < width - 70 && p.y > 110 && p.y < height - 80)
          .sort((a: { y: number }, b: { y: number }) => a.y - b.y)[0] : null
        return { ...outline, path, arrowPositions: arrows, labelPoint }
      })
      if (!arrowsOnly) {
        for (const { path, hatch, dots, alpha = 1 } of paths) {
          if (!hatch && !dots) continue
          const pattern = ctx.createPattern(dots ? dotsTile(dots) : hatchTile(hatch!), 'repeat')
          if (!pattern) continue
          ctx.globalAlpha = opacity * alpha * 0.75
          ctx.fillStyle = pattern
          ctx.fill(path, 'evenodd')
        }
        // 窄衬线保留原有直角和坐标；虚线的衬线也留出同样的间隔。
        ctx.strokeStyle = '#f8fafc'
        for (const { path, weight, dash, alpha = 1, haloWidth = 2 } of paths) {
          ctx.globalAlpha = opacity * alpha * 0.92
          ctx.setLineDash(dash ?? [])
          ctx.lineWidth = weight + haloWidth
          ctx.stroke(path)
        }
        for (const { path, weight, color, dash, alpha = 1 } of paths) {
          ctx.globalAlpha = opacity * alpha
          ctx.strokeStyle = color
          ctx.setLineDash(dash ?? [])
          ctx.lineWidth = weight
          ctx.stroke(path)
        }
        ctx.setLineDash([])
        ctx.font = '12px sans-serif'
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        for (const { label, labelPoint, color } of paths) {
          if (!label || !labelPoint) continue
          const w = ctx.measureText(label).width + 16
          ctx.globalAlpha = opacity
          ctx.fillStyle = '#fffdf9'
          ctx.strokeStyle = '#cdddcf'
          ctx.lineWidth = 1
          ctx.beginPath()
          ctx.roundRect(labelPoint.x - w / 2, labelPoint.y - 12, w, 24, 12)
          ctx.fill(); ctx.stroke()
          ctx.fillStyle = color
          ctx.fillText(label, labelPoint.x, labelPoint.y)
        }
      }
      ctx.setLineDash([])
      ctx.lineJoin = 'round'
      for (const { arrowPositions, color, alpha = 1 } of paths) {
        ctx.globalAlpha = opacity * alpha
        for (const arrow of arrowPositions) {
          ctx.save(); ctx.translate(arrow.x, arrow.y); ctx.rotate(arrow.angle)
          ctx.beginPath(); ctx.moveTo(-4, -3); ctx.lineTo(0, 0); ctx.lineTo(-4, 3)
          ctx.strokeStyle = color; ctx.lineWidth = 3.5; ctx.stroke()
          ctx.strokeStyle = '#fff'; ctx.lineWidth = 1.7; ctx.stroke()
          ctx.restore()
        }
      }
      // 编号仍是百度的可点击标注；外框不要从编号及其浅色边框上穿过去。
      for (const { anchor } of outlines) {
        if (!anchor) continue
        const p = map.pointToPixel(new BMapGL.Point(anchor.lng, anchor.lat))
        ctx.clearRect(p.x - 12, p.y - 12, 27, 27)
      }
    }
    const schedule = () => {
      if (frame) return
      frame = requestAnimationFrame(() => {
        frame = 0
        paint()
      })
    }
    paint()
    const events = ['moving', 'moveend', 'zooming', 'zoomend', 'resize', 'tilt_changed', 'heading_changed']
    events.forEach(event => map.addEventListener(event, schedule))
    const observer = new ResizeObserver(schedule)
    observer.observe(canvas)
    window.addEventListener('resize', schedule)
    return () => {
      cancelAnimationFrame(frame)
      observer.disconnect()
      events.forEach(event => map.removeEventListener(event, schedule))
      window.removeEventListener('resize', schedule)
    }
  }, [mapRef, ready, active, outlines, opacity, arrowsOnly])

  return <canvas ref={canvasRef} className="map-outline-canvas" aria-hidden="true" hidden={!active} />
}
