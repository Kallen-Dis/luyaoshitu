import { useEffect, useId, useRef, useState } from 'react'
import { Icon, type IconName } from './markings/Icon'
import './PageIntro.css'

interface IntroPoint {
  title: string
  text: string
  symbol: IconName | 'ring' | 'grid'
}

interface IntroPage {
  topic: string
  title: string
  lead: string
  points: IntroPoint[]
  note: string
}

const PAGES: IntroPage[] = [
  {
    topic: '选社区',
    title: '从你熟悉的社区开始',
    lead: '先看看家门口，日常生活是否方便。',
    points: [
      { title: '输入小区或地址', text: '在地图上方输入位置，点击「体检」，查看步行生活圈。', symbol: 'pin' },
      { title: '也可以先看看样例', text: '打开「样例」，就能查看现成的地图和体检报告。', symbol: 'eye' },
    ],
    note: '这几张卡片只介绍功能，按自己的节奏翻页就好。',
  },
  {
    topic: '看地图',
    title: '读懂家门口的生活圈',
    lead: '地图上的两种范围，分别告诉你：',
    points: [
      { title: '绿色边界', text: '新版默认展示射线与网格采样融合估计的 15 分钟步行范围；待核实部分单独标出。', symbol: 'ring' },
      { title: '灰色方格', text: '步行 1 公里内，至少到不了菜场、药店或小学中的一类。', symbol: 'grid' },
    ],
    note: '点方格看缺哪类设施；打开「图层 → 查看方法对照」，可以比较原射线边界与超时证据。',
  },
  {
    topic: '去出行',
    title: '选个地方，看看怎么走',
    lead: '买菜、买药、接孩子，都能从「出行」开始。',
    points: [
      { title: '找附近的设施', text: '选择设施类型，再选具体想去的那一家。', symbol: 'pin' },
      { title: '顺路安排几站', text: '按自己的顺序安排目的地，查看路线和步行引导。', symbol: 'route' },
    ],
    note: '「出行」入口就在地图上方，也能从报告的「怎么走」进入。',
  },
  {
    topic: '规划模拟',
    title: '看看生活圈还能怎样改善',
    lead: '报告会解释问题；「规划模拟」可以帮你比较改善方案。',
    points: [
      { title: '试着增建设施', text: '选择设施类型，在地图上放一个拟建点，看看覆盖和评分怎样变化。', symbol: 'facility_extra' },
      { title: '看看封路的影响', text: '在地图上放置假设围挡，比较封路前后的步行范围。', symbol: 'closure' },
    ],
    note: '从地图上方「规划模拟」进入，模拟结果可以和原报告对照。',
  },
  {
    topic: '共享标注',
    title: '把现场发现告诉大家',
    lead: '发现附近的实际情况和地图不同？可以用「共享标注」补充。',
    points: [
      { title: '选择类型和位置', text: '可以标围挡或封路、设施失效，补录设施，或记录灰色区域。', symbol: 'pin' },
      { title: '附照片，写清情况', text: '上传现场照片，写下简短说明，提交后可在地图和报告里查看。', symbol: 'camera' },
    ],
    note: '点击地图上方「共享标注」，按步骤记录你发现的情况。',
  },
  {
    topic: '导出 PDF',
    title: '把体检报告保存下来',
    lead: '用「导出 PDF」保存地图和报告，方便回看或交流。',
    points: [
      { title: '找到导出入口', text: '点击地图上方「导出 PDF」；窗口较小时，打开「更多」也能找到。', symbol: 'eye' },
      { title: '选择保存为 PDF', text: '在打印窗口中选择「另存为 PDF」或 PDF 打印机，再确认保存。', symbol: 'check' },
    ],
    note: '想再看看介绍，可以随时从右上角「一起看看」打开。',
  },
]

function PointSymbol({ symbol }: { symbol: IntroPoint['symbol'] }) {
  if (symbol === 'ring') return <span className="page-intro-ring" aria-hidden="true" />
  if (symbol === 'grid') return <span className="page-intro-grid" aria-hidden="true"><i /><i /><i /><i /></span>
  return <Icon name={symbol} size={24} />
}

/** 独立介绍，不切换业务模式、请求定位或触发任何计算。 */
export function PageIntro({ onClose }: { onClose: () => void }) {
  const [pageIndex, setPageIndex] = useState(0)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const titleRef = useRef<HTMLHeadingElement>(null)
  const titleId = useId()
  const leadId = useId()
  const page = PAGES[pageIndex]
  const last = pageIndex === PAGES.length - 1

  useEffect(() => {
    const dialog = dialogRef.current!
    const trigger = document.activeElement
    // 原生模态负责背景 inert 与焦点约束；透明背景保留地图的可读性。
    dialog.showModal()
    titleRef.current?.focus({ preventScroll: true })
    return () => {
      dialog.close()
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true })
    }
  }, [])

  useEffect(() => { titleRef.current?.focus({ preventScroll: true }) }, [pageIndex])

  function previous() { setPageIndex(index => Math.max(0, index - 1)) }
  function next() {
    if (last) onClose()
    else setPageIndex(index => Math.min(PAGES.length - 1, index + 1))
  }

  return (
    <dialog
      ref={dialogRef}
      className="page-intro"
      aria-labelledby={titleId}
      aria-describedby={leadId}
      onCancel={event => { event.preventDefault(); onClose() }}
      onKeyDown={event => {
        // 介绍打开时，不让键盘事件触发地图或其他抽屉的全局快捷键。
        event.stopPropagation()
        if (event.altKey || event.ctrlKey || event.metaKey) return
        if (event.key === 'Escape') { event.preventDefault(); onClose() }
        if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault()
          if (!event.repeat) {
            if (event.key === 'ArrowLeft') previous()
            else next()
          }
        }
      }}
    >
      <div className="page-intro-shell">
        <button type="button" className="page-intro-arrow page-intro-prev" hidden={pageIndex === 0} aria-label="上一张" title="上一张" onClick={previous}>
          <Icon name="back" size={26} />
        </button>
        <section className="page-intro-card">
          <header className="page-intro-header">
            <span className="page-intro-label">一起看看附近</span>
            <button type="button" className="page-intro-close" aria-label="关闭介绍" title="关闭介绍" onClick={onClose}>
              <Icon name="close" size={21} />
            </button>
          </header>
          <div className="page-intro-copy">
            <h2 id={titleId} ref={titleRef} tabIndex={-1}>{page.title}</h2>
            <p className="page-intro-lead" id={leadId}>{page.lead}</p>
            <ul className="page-intro-points">
              {page.points.map(point => (
                <li key={point.title}>
                  <span className={`page-intro-symbol${point.symbol === 'grid' ? ' is-grid' : ''}`}><PointSymbol symbol={point.symbol} /></span>
                  <div><h3>{point.title}</h3><p>{point.text}</p></div>
                </li>
              ))}
            </ul>
          </div>
          <footer className="page-intro-footer">
            <p className="page-intro-note">{page.note}</p>
            <div className="page-intro-progress" role="status" aria-live="polite" aria-label={`第 ${pageIndex + 1} 张，共 ${PAGES.length} 张`}>
              <span className="page-intro-dots" aria-hidden="true">{PAGES.map((item, index) => <i key={item.topic} className={index === pageIndex ? 'is-current' : undefined} />)}</span>
              <span>{pageIndex + 1} / {PAGES.length}</span>
            </div>
          </footer>
        </section>
        <button type="button" className="page-intro-arrow page-intro-next" aria-label={last ? '完成介绍' : '下一张'} title={last ? '完成介绍' : '下一张'} onClick={next}>
          <Icon name={last ? 'check' : 'chevron'} size={26} />
        </button>
      </div>
    </dialog>
  )
}
