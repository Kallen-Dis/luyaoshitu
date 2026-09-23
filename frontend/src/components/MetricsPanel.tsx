import type { IsochroneProperties } from '../types'

/** 常人步行速度约 1.2 m/s；其它出行方式用结果里带回的速度，直线圆才对得上。 */
const WALK_SPEED_M_PER_S = 1.2

interface Props {
  props: IsochroneProperties
}

/**
 * 指标面板。
 *
 * 核心是把「真实路网面积」与「直线画圆面积」并排摆出——这是命题痛点最直观的量化：
 * 传统的直线缓冲区做法会成倍高估服务覆盖范围。
 */
export function MetricsPanel({ props }: Props) {
  const speed = props.speed_m_per_s ?? WALK_SPEED_M_PER_S
  const idealRadiusM = props.minutes * 60 * speed
  const idealAreaKm2 = (Math.PI * idealRadiusM ** 2) / 1e6
  const ratio = idealAreaKm2 > 0 ? props.area_km2 / idealAreaKm2 : 0
  const modeLabel = props.mode_label ?? '步行'

  return (
    <section className="card">
      <h2 className="card-title">可达范围</h2>

      <div className="metrics-headline">
        <span className="metrics-value">{props.area_km2.toFixed(3)}</span>
        <span className="metrics-unit">平方公里</span>
      </div>
      <p className="metrics-caption">
        {props.minutes} 分钟{modeLabel}真实可达范围
      </p>

      {/* 两条同尺度的条形：直线口径的虚线条有多长，高估就有多少 */}
      <div className="compare">
        <div className="compare-row real">
          <span className="label">真实路网</span>
          <span className="track">
            <i style={{ width: `${Math.min(100, ratio * 100)}%` }} />
          </span>
          <span className="value">{props.area_km2.toFixed(2)} km²</span>
        </div>
        <div className="compare-row ideal">
          <span className="label">直线画圆</span>
          <span className="track">
            <i style={{ width: '100%' }} />
          </span>
          <span className="value">{idealAreaKm2.toFixed(2)} km²</span>
        </div>
      </div>

      <div className="callout">
        直线缓冲区会把覆盖范围算成 <strong>{idealAreaKm2.toFixed(2)} 平方公里</strong>，
        真实路网只有其 <strong>{(ratio * 100).toFixed(0)}%</strong>，
        传统做法高估了 <strong>{(1 / ratio).toFixed(1)} 倍</strong>。
      </div>

      <dl className="metrics-grid">
        <div>
          <dt>平均半径</dt>
          <dd>{props.mean_radius_m.toFixed(0)} 米</dd>
        </div>
        <div>
          <dt>最短方向</dt>
          <dd>{props.min_radius_m.toFixed(0)} 米</dd>
        </div>
        <div>
          <dt>最远方向</dt>
          <dd>{props.max_radius_m.toFixed(0)} 米</dd>
        </div>
        <div>
          <dt>紧凑度</dt>
          <dd>{props.compactness.toFixed(3)}</dd>
        </div>
      </dl>

      {props.factors && props.factors.length > 0 && (
        <ul className="factor-list">
          {props.factors.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      {props.blindspots_skipped && <p className="hint">{props.blindspots_skipped}</p>}

      <p className="metrics-note">
        紧凑度为最短方向半径除以最远方向半径，越接近 1 说明各方向可达性越均衡。
        明显偏低意味着存在铁路、河道或高架造成的可达性切割。
      </p>

      <p className="metrics-meta">
        采样 {props.sampled_points} 点
        {props.failed_points > 0 && (
          <span className="warn">，其中 {props.failed_points} 点算路失败，结果偏保守</span>
        )}
      </p>
    </section>
  )
}
