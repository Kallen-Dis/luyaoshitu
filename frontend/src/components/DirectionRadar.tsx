import type { IsochroneQuality, RayMetric } from '../types'

interface Props {
  rays: RayMetric[]
  quality?: IsochroneQuality
}

/** 各方向可达半径雷达图。纯 SVG，不引入图表库。 */
export function DirectionRadar({ rays, quality }: Props) {
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
    <>
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
      {quality && (
        <ul className="radar-quality">
          <li>共 {quality.directions} 个方向</li>
          {quality.saturated != null && quality.saturated > 0 && (
            <li className="warn">
              {quality.saturated} 个方向采样上界内未超时，真实边界可能更远
            </li>
          )}
          {quality.barrier_truncated > 0 && (
            <li className="warn">
              {quality.barrier_truncated} 个方向被算路失败截断（红点）
            </li>
          )}
          {quality.zero_radius > 0 && (
            <li className="warn">{quality.zero_radius} 个方向首个采样点即不可达</li>
          )}
          {(quality.saturated == null || quality.saturated === 0) &&
            quality.barrier_truncated === 0 &&
            quality.zero_radius === 0 && (
              <li>无截断、无饱和，边界插值完整</li>
            )}
        </ul>
      )}
    </>
  )
}
