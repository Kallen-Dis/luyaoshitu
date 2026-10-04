import type { ExamReport } from '../types'

/** 五维评分与后端 score.py 的权重一一对应。 */
const AXES: { key: keyof ExamReport['dimensions']; label: string; weight: number }[] = [
  { key: 'reach', label: '路网可达', weight: 0.3 },
  { key: 'compact', label: '方向均衡', weight: 0.2 },
  { key: 'detour', label: '路径效率', weight: 0.15 },
  { key: 'cover', label: '设施覆盖', weight: 0.2 },
  { key: 'equity', label: '可达均衡', weight: 0.15 },
]

// 标签画在 1.28 倍半径处，半径取 64 才能让左右两侧的文字不出画布
const SIZE = 240
const C = SIZE / 2
const R = 64

function polar(i: number, r: number): [number, number] {
  const angle = -Math.PI / 2 + (i * 2 * Math.PI) / AXES.length
  return [C + r * Math.cos(angle), C + r * Math.sin(angle)]
}

/** 分值 0~100 映射到半径；超界的分值夹到边上。 */
function point(i: number, value: number): [number, number] {
  return polar(i, (Math.max(0, Math.min(100, value)) / 100) * R)
}

/**
 * 体检评分雷达：五个维度 0~100。没测的维度（如未做设施采集）画在圆心并标「待测」，
 * 不按 0 分画——「没测」和「很差」在图上必须看得出区别。
 */
export function ScoreRadar({ report }: { report: ExamReport }) {
  const values = AXES.map((a) => report.dimensions[a.key])
  const polygon = values.map((v, i) => point(i, v ?? 0).join(',')).join(' ')
  const described = AXES.map(
    (a, i) => `${a.label} ${values[i] == null ? '待测' : Math.round(values[i] as number)}`,
  ).join('，')

  return (
    <div className="score-radar">
      <svg
        viewBox={`0 0 ${SIZE} ${SIZE}`}
        width={SIZE}
        height={SIZE}
        role="img"
        aria-label={`五维评分雷达：${described}`}
      >
        {[0.25, 0.5, 0.75, 1].map((t) => (
          <polygon
            key={t}
            points={AXES.map((_, i) => point(i, t * 100).join(',')).join(' ')}
            fill="none"
            stroke="#d6dbd3"
            strokeWidth={1}
          />
        ))}
        {AXES.map((_, i) => {
          const [x, y] = point(i, 100)
          return <line key={i} x1={C} y1={C} x2={x} y2={y} stroke="#e3e7e0" strokeWidth={1} />
        })}
        <polygon points={polygon} fill="rgb(47 158 90 / 28%)" stroke="#1f7a42" strokeWidth={2} />
        {values.map((v, i) => {
          const [x, y] = point(i, v ?? 0)
          return (
            <circle
              key={AXES[i].key}
              cx={x}
              cy={y}
              r={3.5}
              fill={v == null ? '#fff' : '#1f7a42'}
              stroke="#1f7a42"
            />
          )
        })}
        {AXES.map((a, i) => {
          // 标签不走 point()：那里会把半径夹在 100 分的圈上，文字就压到图形边缘了
          const [x, y] = polar(i, R * 1.28)
          const v = values[i]
          return (
            <text
              key={a.key}
              x={x}
              y={y}
              textAnchor="middle"
              dominantBaseline="middle"
              fontSize={11}
              fill={v == null ? '#9ca3af' : '#1f2a1f'}
            >
              {a.label} {v == null ? '待测' : Math.round(v)}
            </text>
          )
        })}
      </svg>
      <ul className="score-dims">
        {AXES.map((a, i) => (
          <li key={a.key}>
            <span>
              {a.label}
              <em>权重 {Math.round(a.weight * 100)}%</em>
            </span>
            <span className="score-bar">
              <i style={{ width: `${values[i] ?? 0}%` }} />
            </span>
            <b>{values[i] == null ? '待测' : Math.round(values[i] as number)}</b>
          </li>
        ))}
      </ul>
    </div>
  )
}
