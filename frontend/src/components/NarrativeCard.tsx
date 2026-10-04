import { useEffect, useState } from 'react'
import { narrate } from '../api'
import type { IsochroneFeature, Narrative } from '../types'

/** 把「【小标题】正文」拆开，小标题加粗。文字按纯文本渲染，不当 HTML 解析。 */
function paragraphs(text: string) {
  return text
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = line.match(/^【([^】]{1,12})】(.*)$/)
      return m ? { head: m[1], body: m[2] } : { head: null, body: line }
    })
}

interface Entry {
  owner: IsochroneFeature
  narrative: Narrative | null
  error: string | null
}

/**
 * 综合结论：把评分、灰色区域与处方写成四段话。结果一到就自动生成，不用再点按钮。
 * 由后端按模板从算法结果生成：不调外部服务、不花配额，同一份结果永远是同一段话，导出报告里也是这段。
 */
export function NarrativeCard({ feature }: { feature: IsochroneFeature }) {
  const [entry, setEntry] = useState<Entry | null>(null)

  useEffect(() => {
    let alive = true
    narrate(feature)
      .then((narrative) => {
        if (alive) setEntry({ owner: feature, narrative, error: null })
      })
      .catch((err: unknown) => {
        if (alive) {
          const error = err instanceof Error ? err.message : String(err)
          setEntry({ owner: feature, narrative: null, error })
        }
      })
    return () => {
      alive = false
    }
  }, [feature])

  // 实时分析逐段推送时结果会变几次：新的一段还没写好前，先留着上一段，避免卡片闪空
  const updating = entry?.owner !== feature
  const narrative = entry?.narrative ?? null

  return (
    <section className="card narrative-card" aria-live="polite" aria-busy={updating}>
      <h2 className="card-title">综合结论</h2>
      {narrative ? (
        <div className={updating ? 'narrative-text updating' : 'narrative-text'}>
          {paragraphs(narrative.text).map((p, i) => (
            <p key={i}>
              {p.head && <b>【{p.head}】</b>}
              {p.body}
            </p>
          ))}
        </div>
      ) : entry?.error && !updating ? (
        <p className="hint error-text" role="alert">
          综合结论没有生成：{entry.error}
        </p>
      ) : (
        <p className="hint">正在整理结论…</p>
      )}
    </section>
  )
}
