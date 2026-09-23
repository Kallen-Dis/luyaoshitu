const GRADE_COLOR: Record<string, string> = {
  优: '#1a7f37',
  良: '#1f6feb',
  中: '#bf8700',
  弱: '#cf222e',
}

interface Props {
  total: number
  grade: string
}

/** 总分环形图。纯 SVG，避免为一个读数引入图表库。 */
export function ScoreGauge({ total, grade }: Props) {
  const radius = 40
  const circumference = 2 * Math.PI * radius
  const filled = Math.max(0, Math.min(100, total)) / 100

  return (
    <svg className="gauge" viewBox="0 0 100 100" role="img" aria-label={`体检总分 ${total}`}>
      <circle className="gauge-track" cx="50" cy="50" r={radius} />
      <circle
        className="gauge-arc"
        cx="50"
        cy="50"
        r={radius}
        stroke={GRADE_COLOR[grade] ?? '#1f6feb'}
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - filled)}
        transform="rotate(-90 50 50)"
      />
      <text className="gauge-total" x="50" y="53">
        {total.toFixed(0)}
      </text>
      <text className="gauge-cap" x="50" y="67">
        总分 / 100
      </text>
    </svg>
  )
}
