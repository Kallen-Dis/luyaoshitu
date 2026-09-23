import type { IsochroneFeature } from '../types'

interface Props {
  a: IsochroneFeature
  b: IsochroneFeature
  onClose: () => void
}

/** 一行指标：A / B 两列 + 简短差值或结论。 */
function Row({
  label,
  a,
  b,
  invert,
  fmt,
}: {
  label: string
  a: number | null | undefined
  b: number | null | undefined
  /** true 表示数值越大越差（如盲区占比）。 */
  invert?: boolean
  fmt?: (v: number) => string
}) {
  const format = fmt ?? ((v: number) => String(Math.round(v * 10) / 10))
  const betterA = a != null && b != null && (invert ? a < b : a > b)
  return (
    <li>
      <span className="cmp-label">{label}</span>
      <b className={a != null && b != null && betterA ? 'cmp-good' : ''}>
        {a != null ? format(a) : '—'}
      </b>
      <b className={a != null && b != null && !betterA && a !== b ? 'cmp-bad' : ''}>
        {b != null ? format(b) : '—'}
      </b>
    </li>
  )
}

/**
 * 两地对比卡：两个样例（或实时结果 + 样例）的关键指标并排对照。
 * 数据都来自已加载的 Feature，零 API 消耗。
 */
export function CompareCard({ a, b, onClose }: Props) {
  const nameA = (a.properties.name ?? 'A').replace(/^上海市普陀区/, '')
  const nameB = (b.properties.name ?? 'B').replace(/^上海市普陀区/, '')
  const ra = a.properties.report
  const rb = b.properties.report
  if (!ra || !rb) return null

  const inA = Object.values(a.properties.coverage?.categories ?? {}).reduce((s, n) => s + n, 0)
  const inB = Object.values(b.properties.coverage?.categories ?? {}).reduce((s, n) => s + n, 0)

  const blindA = ra.blind_cell_count
  const blindB = rb.blind_cell_count

  // 结论模板：先说差距最大的一项，再给方向性判断
  const gaps: string[] = []
  if (blindA != null && blindB != null && blindA !== blindB) {
    gaps.push(
      `${blindA < blindB ? nameA : nameB}的盲区网格更少（${Math.min(blindA, blindB)} / ${
        Math.max(blindA, blindB)
      }）`,
    )
  }
  if (inA !== inB) {
    gaps.push(`${inA > inB ? nameA : nameB}圈内设施更多（${Math.max(inA, inB)} 处对 ${Math.min(inA, inB)} 处）`)
  }
  if (ra.area_ratio !== rb.area_ratio) {
    gaps.push(
      `${ra.area_ratio < rb.area_ratio ? nameA : nameB}被路网切割更严重（真实面积仅占直线圆的 ${(
        Math.min(ra.area_ratio, rb.area_ratio) * 100
      ).toFixed(0)}%）`,
    )
  }

  return (
    <section className="card">
      <h2 className="card-title">两地对比</h2>
      <p className="hint">
        A = 当前结果，B = 对比样例。地图上已同时渲染两圈（A 蓝 / B 橙）。
      </p>
      <ul className="cmp-table">
        <li className="cmp-head">
          <span className="cmp-label">指标</span>
          <b>A · {nameA}</b>
          <b>B · {nameB}</b>
        </li>
        <Row label="体检总分" a={ra.total} b={rb.total} />
        <Row
          label="真实面积"
          a={a.properties.area_km2}
          b={b.properties.area_km2}
          fmt={(v) => `${v.toFixed(2)} km²`}
        />
        <Row
          label="占直线画圆"
          a={ra.area_ratio}
          b={rb.area_ratio}
          fmt={(v) => `${(v * 100).toFixed(0)}%`}
        />
        <Row label="圈内设施" a={inA} b={inB} />
        <Row
          label="盲区网格"
          a={blindA}
          b={blindB}
          invert
          fmt={(v) => String(v)}
        />
        <Row label="紧凑度" a={a.properties.compactness} b={b.properties.compactness} fmt={(v) => v.toFixed(2)} />
      </ul>
      {gaps.length > 0 && (
        <p className="cmp-story">
          对比结论：{gaps.join('；')}。直线缓冲法会把两地都算成"配套齐全"，差别恰恰在路网能否走通。
        </p>
      )}
      <button type="button" className="sim-clear cmp-close" onClick={onClose}>
        退出对比
      </button>
    </section>
  )
}
