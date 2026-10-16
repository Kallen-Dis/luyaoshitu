import type { FusionHole } from '../types'

export function HoleEvidence({ hole, accepted = false, localEstimate = false }: { hole: FusionHole; accepted?: boolean; localEstimate?: boolean }) {
  if (hole.status === 'geographic') return <div className="hint">
    <p>水域边界排除区 · 约 {Math.round(hole.area_m2).toLocaleString()} m²</p>
    <p>依据地理边界数据排除，不属于居民设施盲区，也不表示路线超时。</p>
  </div>
  return <div className="hint">
    <p>{localEstimate ? '小范围局部排除估计' : accepted ? '路线超时支持的内部排除区' : '尚未采用的内圈候选'} · 约 {Math.round(hole.area_m2).toLocaleString()} m²</p>
    <p>内部 {hole.slow_points} 个分散超时点，外围 {hole.surrounding_sectors.length} 个方向有可达证据。</p>
    <p>局部加密：{hole.mesh_stable ? '稳定' : '未通过'}；影响范围检查：{hole.support_stable ? '稳定' : '未通过'}。</p>
    <p>{hole.confirmed_clue_ids.length ? `关联已核验绕行线索：${hole.confirmed_clue_ids.join('、')}` : '暂无符合位置与方向要求的已核验绕行线索。'}</p>
    <p>{localEstimate ? '保留可靠超时点，边界尚未独立核实；局部差异不否定整份融合估计。' : accepted ? '已完成独立路线核验。' : '仍需证据及独立验收通过后才能采用。'}这是一片采样支持的估计区域，不表示其中每个门口都已实测。</p>
  </div>
}
