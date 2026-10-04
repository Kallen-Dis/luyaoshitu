import type { CrosscheckResult, CrosscheckRow, CrosscheckSuspect } from '../types'
import { SHORT_NAME } from './map/context'

interface Props {
  result: CrosscheckResult | null
  busy: boolean
  onRun: () => void
  /** 按补录模拟：缺这一类、直线 1 公里内的方格到它做一次路网测距 */
  onSimulate: (s: CrosscheckSuspect) => void
  /** 共享为「补录设施」标注；没开共享标注时不给这个按钮 */
  onShare?: (s: CrosscheckSuspect) => void
}

const shortName = (category: string) => SHORT_NAME[category] ?? category

/** 一行核对结果：已收录几处、疑似漏收录几处，其余是离缺口太远、不影响结论的。 */
function rowSummary(r: CrosscheckRow) {
  if (r.found.length === 0) return '附近没有返回同类设施'
  const far = r.found.length - r.matched - r.suspects
  return (
    `找到 ${r.found.length} 处同类：已收录 ${r.matched} 处` +
    (r.suspects > 0 ? `，疑似漏收录 ${r.suspects} 处` : '') +
    (far > 0 ? `，另 ${far} 处离缺口格直线超过 1 公里（不影响结论）` : '')
  )
}

/**
 * AI 二次核对：用百度地图 Agent Plan 的语义地点检索，再找一遍灰色区域附近缺的关键设施。
 * 关键词检索漏掉设施会凭空造出盲区；两条召回通道对得上，盲区结论才更可信。只给线索，不改结论。
 */
export function CrosscheckPanel({ result, busy, onRun, onSimulate, onShare }: Props) {
  return (
    <div className="crosscheck" aria-live="polite">
      <div className="crosscheck-head">
        <button type="button" className="export-btn primary" disabled={busy} onClick={onRun}>
          {busy ? '核对中…' : result ? '重新核对' : 'AI 二次核对'}
        </button>
        <span>百度地图 Agent Plan 语义检索 · 同样的问题只问一次</span>
      </div>

      {!result && (
        <p className="hint">
          关键词检索漏掉设施，会凭空造出盲区。对每片灰色区域缺的关键设施，再用 Agent Plan
          问一次「附近最近的菜场 / 药店 / 小学」，和已收录的设施逐个比对，列出疑似漏收录的。
        </p>
      )}

      {result && (
        <>
          <ul className="crosscheck-rows">
            {result.rows.map((r) => (
              <li key={`${r.region}-${r.category}`}>
                <b>
                  {r.region} · 缺{shortName(r.category)}
                </b>
                {r.error ? (
                  <span className="crosscheck-error">没核对成功：{r.error}</span>
                ) : (
                  <span>{rowSummary(r)}</span>
                )}
              </li>
            ))}
          </ul>

          {result.suspects.length > 0 ? (
            <ul className="crosscheck-suspects">
              {result.suspects.map((s) => (
                <li key={`${s.region}-${s.name}-${s.lat}-${s.lng}`}>
                  <div>
                    <b>{s.name}</b>
                    <span>
                      疑似漏收录 · {s.region} 片缺{shortName(s.category)} · 离最近的缺口格直线{' '}
                      {s.nearest_gap_m} 米
                    </span>
                  </div>
                  <div className="crosscheck-actions">
                    <button
                      type="button"
                      className="export-btn"
                      title="把它当成已有设施，对缺这一类、直线 1 公里内的方格做一次路网测距（花少量批量算路点对）"
                      onClick={() => onSimulate(s)}
                    >
                      按补录测一下
                    </button>
                    {onShare && (
                      <button type="button" className="export-btn" onClick={() => onShare(s)}>
                        共享为补录标注
                      </button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            !result.aborted &&
            result.rows.some((r) => !r.error) && (
              <p className="crosscheck-ok">
                两条召回通道没有分歧：缺口附近的同类设施都已收录，这些盲区不是关键词漏检造成的。
              </p>
            )
          )}

          <p className="hint">
            在「{result.region_name}」范围内问了 {result.rows.length} 个问题：新问{' '}
            {result.agent_plan.requests} 个，读缓存 {result.agent_plan.cache_hits} 个。
            {result.skipped > 0
              ? `另有 ${result.skipped} 个超出单次 ${result.max_questions} 个问题的上限，没有问。`
              : ''}
            地图上查得到不等于正在营业，补录前请到现场确认。
          </p>
        </>
      )}
    </div>
  )
}
