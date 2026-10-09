import type { TripItem } from '../../types'
import { tripMeters, tripMinutes } from '../../lib/trip'
import { guideUnavailableReason } from '../../lib/guide'

export function TripSteps({ item, onGuide, preview, launchLabel = '步行引导', showLaunch = true }: { item: TripItem; onGuide: (item: TripItem) => void; preview?: boolean; launchLabel?: string; showLaunch?: boolean }) {
  const reason = guideUnavailableReason(item, preview)
  const available = reason === null
  return <div className="trip-details">
    {item.note && <p className={item.closure_status === 'blocked' ? 'trip-blocked' : 'trip-note'}>{item.note}</p>}
    {showLaunch && <button type="button" className="trip-guide-launch" disabled={!available} onClick={() => onGuide(item)}>{launchLabel} <span>{available ? `沉浸查看 · ${item.route?.steps.length} 步` : reason}</span></button>}
    <details className="trip-directions"><summary>文字步骤 <span>{item.route ? `${item.route.steps.length} 步` : '暂未取得折线'}</span></summary>
    <p className="trip-note">{item.distance_basis === 'gate' ? `入口：${item.gate}` : item.distance_basis === 'navigation_point' ? '按导航点测距' : '按设施坐标测距'}
      {item.duration_basis === 'matrix' ? ' · 耗时未含过街等待' : item.duration_basis === 'estimate' ? ' · 时间为估算' : ' · 时间为百度预计耗时'}</p>
    {item.route?.connectors.length ? <p className="trip-note">灰色短虚线为端点连接，尚未核验通行。</p> : null}
    {item.route && <ol className="trip-steps">{item.route.steps.map((step, index) => <li key={index}>
      <span aria-hidden="true">{/天桥/.test(step.instruction) ? '↗' : /地道|地下通道/.test(step.instruction) ? '↘' : /过马路|斑马线|人行横道|斜对面/.test(step.instruction) ? '↔' : '·'}</span>
      <div>{step.instruction}<small>{tripMeters(step.distance_m)} · 约 {tripMinutes(step.duration_s)}</small></div>
    </li>)}</ol>}
    </details>
  </div>
}
