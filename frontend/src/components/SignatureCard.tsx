import type { ExamReport, IsochroneProperties } from '../types'

/**
 * 图签栏：这份结果的可追溯信息（中心点、坐标系、采样、数据来源、生成时间）。
 * 按工程图纸的惯例放在侧栏最末，当作页脚；数据来源与生成时间另在顶部结论卡片里写一行。
 */
export function SignatureCard({
  meta,
  report,
}: {
  meta: IsochroneProperties
  report: ExamReport
}) {
  return (
    <section className="card signature" aria-label="图签：结果的可追溯信息">
      <dl className="sig-grid">
        <div>
          <dt>图名</dt>
          <dd>{meta.minutes} 分钟生活圈体检报告</dd>
        </div>
        <div>
          <dt>中心点</dt>
          <dd>
            {meta.center ? `${meta.center.lat.toFixed(5)}, ${meta.center.lng.toFixed(5)}` : '—'}
          </dd>
        </div>
        <div>
          <dt>坐标系</dt>
          <dd>
            {meta.input_coord_sys && meta.input_coord_sys !== 'bd09'
              ? `BD-09（${meta.input_coord_sys.toUpperCase()} 输入）`
              : 'BD-09（百度）'}
          </dd>
        </div>
        <div>
          <dt>出行方式</dt>
          <dd>{meta.mode_label ?? '步行'}</dd>
        </div>
        <div>
          <dt>采样</dt>
          <dd>
            {meta.sampled_points} 点 · {meta.rays?.length ?? 0} 方向
          </dd>
        </div>
        <div>
          <dt>数据来源</dt>
          <dd>
            {meta.simulated ? '离线模拟（非真实路网）' : (report.coverage_source ?? '实时计算')}
          </dd>
        </div>
        <div>
          <dt>生成时间</dt>
          <dd>
            {meta.generated_at
              ? new Date(meta.generated_at).toLocaleString('zh-CN', { hour12: false })
              : '实时计算'}
          </dd>
        </div>
        <div>
          <dt>出图</dt>
          <dd>路遥识途 · 真实路网口径</dd>
        </div>
      </dl>
    </section>
  )
}
