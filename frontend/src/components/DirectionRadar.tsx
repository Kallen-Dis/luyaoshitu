import type { RayMetric } from '../types'

interface Props {
  rays: RayMetric[]
}

/** 各方向可达半径雷达图。纯 SVG，不引入图表库。 */
export function DirectionRadar({ rays }: Props) {
  if (rays.length < 3) return null
  const size = 220
  const cx = size / 2
  const cy = size / 2
  const maxR = Math.max(...rays.map((r) => r.radius_m), 1)
  const ring = size * 0.38

  const point = (bearing: number, radiusM: number) => {
    const rad = (bearing * Math.PI) / 180
    const t = radiusM / maxR
    return [cx + Math.sin(rad) * ring * t, cy - Math.cos(rad) * ring * t]
  }

  const poly = rays
    .map((r) => point(r.bearing, r.radius_m).map((n) => n.toFixed(1)).join(','))
    .join(' ')

  const ticks = [0.33, 0.66, 1]
  const labels = [
    { t: '北', x: cx, y: 14 },
    { t: '东', x: size - 14, y: cy + 4 },
    { t: '南', x: cx, y: size - 4 },
    { t: '西', x: 14, y: cy + 4 },
  ]

  return (
    <svg className="radar" viewBox={`0 0 ${size} ${size}`} role="img" aria-label="各方向可达半径">
      {ticks.map((t) => (
        <circle key={t} cx={cx} cy={cy} r={ring * t} className="radar-ring" />
      ))}
      <line x1={cx} y1={cy - ring} x2={cx} y2={cy + ring} className="radar-axis" />
      <line x1={cx - ring} y1={cy} x2={cx + ring} y2={cy} className="radar-axis" />
      <polygon points={poly} className="radar-fill" />
      {rays
        .filter((r) => r.barrier)
        .map((r) => {
          const [x, y] = point(r.bearing, r.radius_m)
          return <circle key={r.bearing} cx={x} cy={y} r={2.5} className="radar-barrier" />
        })}
      {labels.map((l) => (
        <text key={l.t} x={l.x} y={l.y} className="radar-label">
          {l.t}
        </text>
      ))}
    </svg>
  )
}
