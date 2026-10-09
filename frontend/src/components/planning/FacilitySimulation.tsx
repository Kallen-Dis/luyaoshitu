import type { SimulationResult } from '../../types'
import { TripCategoryPicker } from '../trip/TripCategoryPicker'

export function FacilitySimulation({ category, result, busy, onCategory }: {
  category: string; result: SimulationResult | null; busy: boolean
  onCategory: (category: string) => void
}) {
  return <>
    <section className="planning-section" aria-labelledby="planning-category">
      <h3 id="planning-category">设施类别<span>{category}</span></h3>
      <TripCategoryPicker value={category} failed={[]} onChange={onCategory} label="选择拟建设施类别" />
    </section>
    <div className="planning-placement">
      <span className="planning-point" aria-hidden="true">＋</span><div>
        <strong>{result ? `拟建${category}` : `选择${category}的位置`}</strong>
        <p>{result ? `${result.lat.toFixed(6)}, ${result.lng.toFixed(6)}` : '在地图上点一下放置设施。'}</p>
        <small>再次点选地图可以调整位置。</small>
      </div>
    </div>
    <div className="trip-status" role="status" aria-live="polite">{busy ? '正在评估拟建点…' : result ? '方案已评估，可以继续比较其他位置。' : '放置后查看前后评分和覆盖变化。'}</div>
    {result && <section className="planning-section planning-result" aria-labelledby="planning-result-title">
      <h3 id="planning-result-title">方案效果<span>{busy ? '上次结果' : '当前拟建点'}</span></h3>
      <div className="planning-scores">
        <div><span>模拟前</span><strong>{result.before.score ?? '—'}</strong><small>{result.before.grade ?? '暂无评分'}</small></div>
        <span className="planning-score-arrow" aria-hidden="true">→</span>
        <div className="planning-score-after"><span>模拟后</span><strong>{result.after.score ?? '—'}</strong><small>{result.after.grade ?? '暂无评分'}</small></div>
      </div>
      <dl className="planning-impact">
        <div><dt>圈内该类设施</dt><dd>{result.facility_count_before ?? '—'} → {result.facility_count_after ?? '—'} 处</dd></div>
        {result.grid_evaluated !== false && <div><dt>该类缺失网格改善</dt><dd>{result.covered_count} 格{result.candidate_count > 0 ? result.basis === 'network' ? ' · 路网核验' : ' · 估算上限' : ''}</dd></div>}
      </dl>
      {result.inside_closure ? <p className="trip-inline-notice blocked">拟建点位于已知围挡内，暂不计入可用设施。</p> : result.in_circle === false ? <p className="trip-note">拟建点在生活圈外，圈内该类设施数量保持不变。</p> : null}
      <p className="trip-note">{result.approximation}</p>
      {result.before.score === result.after.score && <p className="trip-note">评分不变可能是该类已有覆盖，或拟建点没有改善已判定的缺失网格。</p>}
    </section>}
  </>
}
