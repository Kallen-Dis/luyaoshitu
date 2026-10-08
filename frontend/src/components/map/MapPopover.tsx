import { freshEligible, freshLabel } from '../../lib/fresh'
import { FreshFeedback } from '../trip/FreshFeedback'
import { type CSSProperties, type ReactNode, useEffect, useRef, useState } from 'react'
import { TYPE_META, formatDistance, metersBetween, type LatLng } from '../../lib/markings'
import type { GrayRegion, GridCell, MarkingType, Place, RecheckSuspect } from '../../types'
import { Icon, type IconName } from '../markings/Icon'
import { PLACE_MARK, SHORT_NAME, type ComposerPreset, type MapContext, type MapIntent } from './context'

interface Props {
  ctx: MapContext
  /** 锚点在地图容器里的像素位置 */
  x: number
  y: number
  shellW: number
  shellH: number
  /** 共享标注可用时才给出标注动作 */
  canMark: boolean
  canTrip?: boolean
  feedbackDisabled?: boolean
  /** 盲区判定的步行阈值（米） */
  limitM: number
  /** 当前结果里的设施点，用来说明直线最近的那一家 */
  places: Place[]
  onIntent: (intent: MapIntent) => void
  onClose: () => void
}

const WIDTH = 300
const GAP = 14
const MARGIN = 10

interface Action {
  key: string
  icon: IconName
  color: string
  soft: string
  label: string
  tag: string
  intent: MapIntent
}

interface Content {
  badge?: { glyph: string; color: string }
  title: string
  sub?: string
  body?: ReactNode
  ask?: string
  actions: Action[]
  foot?: string
}

const CAUSE_LABEL = { barrier: '路网阻隔', supply: '供给缺口' } as const

function short(category: string) {
  return SHORT_NAME[category] ?? category
}

function compose(type: MarkingType, label: string, tag: string, preset: Omit<ComposerPreset, 'type'>): Action {
  const meta = TYPE_META[type]
  return {
    key: `${type}-${label}`,
    icon: type,
    color: meta.color,
    soft: meta.soft,
    label,
    tag,
    intent: { kind: 'compose', preset: { type, ...preset } },
  }
}

/** 到设施的直线距离：有入口（校门）按最近的入口算，和后端的剪枝、成因诊断同一口径。 */
function straightTo(at: LatLng, place: Place) {
  const points = place.entries?.length ? place.entries : [place]
  return Math.min(...points.map((p) => metersBetween(at, p)))
}

function nearestOf(places: Place[], category: string, at: LatLng) {
  let best: { place: Place; d: number } | null = null
  for (const place of places) {
    if (place.category !== category || !freshEligible(place)) continue
    const d = straightTo(at, place)
    if (!best || d < best.d) best = { place, d }
  }
  return best
}

/** 「按东南1门、东南2门测距」这类说明；只有导航点时写「按导航入口测距」。 */
function entryNote(place: Place) {
  const names = (place.entries ?? []).map((e) => (e.name === '导航点' ? '导航入口' : e.name))
  return names.length ? `按${names.join('、')}测距，不按地图上的定位点` : null
}

function cellContent(
  cell: GridCell,
  missing: string[],
  places: Place[],
  limitM: number,
  canMark: boolean,
): Content {
  const at = { lat: cell.lat, lng: cell.lng }
  const notes: string[] = []
  const freshReason = cell.unknown_reasons?.['生鲜采买']
  if (freshReason === 'fresh_pending') notes.push('附近超市可达，但是否卖菜待确认')
  else if (freshReason === 'fresh_pending_distance_unknown') notes.push('附近超市是否卖菜待确认，步行测距也未完成')
  else if (freshReason === 'fresh_legacy') notes.push('旧快照的买菜能力待确认，请重新体检')
  const distanceUnknown = cell.unknown.filter(category => category !== '生鲜采买' || !freshReason)
  if (distanceUnknown.length > 0) notes.push(`测距失败、无法判定：${distanceUnknown.map(short).join('、')}`)
  if (cell.closure_blocked) notes.push('从中心过来的路线被围挡挡住')
  else if (cell.reach_s != null) notes.push(`自中心步行约 ${Math.round(cell.reach_s / 60)} 分钟`)
  if (cell.in_circle === false) notes.push('在 15 分钟圈外')

  const body = (
    <>
      {missing.length > 0 && (
        <ul className="map-pop-facts">
          {missing.map((cat) => {
            const walk = cell.nearest_m[cat] ?? null
            const near = nearestOf(places, cat, at)
            // 直线 1 公里内有同类却走不到：路网阻隔；直线都没有：供给缺口。没有设施列表就不判
            const cause = places.length === 0 ? null : near && near.d <= limitM ? 'barrier' : 'supply'
            return (
              <li key={cat}>
                <div>
                  <b>{short(cat)}</b>
                  <span>
                    {walk != null
                      ? `测过的最近一家步行 ${formatDistance(walk)}`
                      : near
                        ? `直线最近 ${formatDistance(near.d)}，超出 ${formatDistance(limitM)}`
                        : '附近没有检索到'}
                  </span>
                  {cause && <em className={`map-pop-cause ${cause}`}>{CAUSE_LABEL[cause]}</em>}
                </div>
                {near && walk != null && (
                  <small>
                    直线最近「{near.place.name}」{formatDistance(near.d)}
                  </small>
                )}
              </li>
            )
          })}
        </ul>
      )}
      {notes.length > 0 && <p className="map-pop-note">{notes.join(' · ')}</p>}
    </>
  )

  const context = '来自地图上的方格'
  const actions: Action[] = canMark
    ? [
        ...missing.slice(0, 3).map((cat) =>
          compose('facility_extra', `这里其实有${short(cat)}`, '补录设施', {
            category: cat,
            point: at,
            context,
          }),
        ),
        compose('closure', '路走不通，要绕很远', '围挡 / 封路', { context }),
        compose('gray_area', '确实缺，另有原因', '灰色区域', {
          categories: missing,
          context,
        }),
      ]
    : []

  return {
    title: missing.length > 0 ? `这一格缺${missing.map(short).join('、')}` : '这一格',
    sub: `步行 ${formatDistance(limitM)} 内到不了，逐格实测路网`,
    body,
    ask: '你看到的情况是？',
    actions,
  }
}

function regionContent(region: GrayRegion, canMark: boolean): Content {
  const body = (
    <ul className="map-pop-facts">
      {region.diagnosis.map((d) => {
        const split = [
          d.supply_cells ? `供给缺口 ${d.supply_cells}` : '',
          d.barrier_cells ? `路网阻隔 ${d.barrier_cells}` : '',
        ].filter(Boolean)
        return (
          <li key={d.category}>
            <div>
              <b>{short(d.category)}</b>
              <span>
                缺 {d.cells} 格{split.length > 0 ? `（${split.join('、')}）` : ''}
              </span>
              {d.cause !== 'unknown' && (
                <em className={`map-pop-cause ${d.cause}`}>{CAUSE_LABEL[d.cause]}</em>
              )}
            </div>
            {d.barrier_cells > 0 && d.nearby && (
              <small>
                往{d.nearby.direction}直线 {d.nearby.straight_m} 米就有「{d.nearby.place}」
                {d.nearby.walk_m != null ? `，步行却要 ${Math.round(d.nearby.walk_m)} 米` : ''}
              </small>
            )}
          </li>
        )
      })}
    </ul>
  )
  return {
    badge: { glyph: region.id ?? '·', color: '#374151' },
    title: region.label,
    sub:
      `${region.cells} 格 · 约 ${region.area_km2} km²` +
      (region.in_circle_cells < region.cells ? ` · ${region.in_circle_cells} 格在 15 分钟圈内` : ''),
    body,
    actions: [],
    foot: canMark ? '点区域里的方格，可以就地标注看到的情况' : undefined,
  }
}

function placeContent(place: Place, canMark: boolean, feedbackDisabled = false): Content {
  const mark = PLACE_MARK[place.category] ?? { glyph: '·', color: '#4b4e45' }
  const where = place.in_circle ? '在 15 分钟圈内' : '在附近，走不进 15 分钟圈'
  if (place.source === 'user') {
    return {
      badge: mark,
      title: place.name,
      sub: `${place.category} · ${where}`,
        body: <><p className="map-pop-note">{place.fresh_status ? `${freshLabel(place.fresh_status)}：${place.fresh_evidence}` : '用户补录的设施，已计入这次分析。'}</p>{canMark && place.category === '生鲜采买' && <FreshFeedback place={place} disabled={feedbackDisabled} />}</>,
      actions:
        place.marking_id != null
          ? [
              {
                key: 'view',
                icon: 'eye',
                color: '#1f7a42',
                soft: '#ecfdf3',
                label: '查看这条标注',
                tag: '详情',
                intent: { kind: 'select-marking', id: place.marking_id },
              },
            ]
          : [],
    }
  }
  const context = '来自地图上的设施'
  const point = { lat: place.lat, lng: place.lng }
  const reasons: [string, string][] = [
    ['closed', '已经关了'],
    ['not_public', '不对外开放'],
    ['wrong_location', '位置或类别不对'],
  ]
  const note = entryNote(place)
  return {
    badge: mark,
    title: place.name,
    sub: `${place.category} · ${where}`,
    body: <>{note && <p className="map-pop-note">{note}。</p>}{place.fresh_status && <p className="map-pop-note">{freshLabel(place.fresh_status)}：{place.fresh_evidence}</p>}{canMark && place.category === '生鲜采买' && <FreshFeedback place={place} disabled={feedbackDisabled} />}</>,
    ask: canMark ? '这家设施有问题？' : undefined,
    actions: canMark
      ? [...reasons.map(([reason, label]) =>
          compose('facility_missing', label, '设施失效', { place, point, reason, context }),
        )]
      : [],
  }
}

function clueActions(
  key: string,
  where: LatLng,
  radius: number,
  label: string,
  source: 'recheck' | 'poi',
  canMark: boolean,
): Action[] {
  const closure = TYPE_META.closure
  // 传进来的可能是整条线索记录，只取坐标，别把多余字段带进标注
  const at = { lat: where.lat, lng: where.lng }
  return [
    {
      key: 'temp',
      icon: 'closure',
      color: '#9a3412',
      soft: '#fff4ec',
      label: '确认为临时围挡',
      tag: '只算这次',
      intent: { kind: 'temp-closure', key, lat: at.lat, lng: at.lng, radius, label },
    },
    ...(canMark
      ? [
          {
            key: 'share',
            icon: 'closure' as const,
            color: closure.color,
            soft: closure.soft,
            label: '共享为围挡标注',
            tag: '附近的人也能用',
            intent: {
              kind: 'compose' as const,
              preset: {
                type: 'closure' as const,
                point: at,
                radius,
                source,
                context: source === 'recheck' ? '来自复测巡检' : '来自工地检索',
              },
            },
          },
        ]
      : []),
    {
      key: 'dismiss',
      icon: 'close',
      color: '#6b7280',
      soft: '#f3f4f6',
      label: '不是阻断，忽略',
      tag: '',
      intent: { kind: 'dismiss', key },
    },
  ]
}

function suspectContent(s: RecheckSuspect, canMark: boolean): Content {
  return {
    badge: { glyph: '疑', color: '#7c3aed' },
    title: s.label,
    sub: `路线变长 ${s.max_delta_m} 米 · 半径 ${s.radius_m} 米${s.precise ? '' : ' · 位置较粗'}`,
    body: <p className="map-pop-note">{s.reason}</p>,
    ask: '现场确实被挡住了吗？',
    actions: clueActions(s.id, s, s.radius_m, s.label, 'recheck', canMark),
  }
}

function pointContent(lat: number, lng: number): Content {
  const at = { lat, lng }
  const context = '来自地图上点的位置'
  return {
    title: '在这里标注',
    sub: '地图上缺了什么，或者哪里和现场不一样？',
    actions: [
      compose('facility_extra', '这里有设施，地图上没有', '补录设施', { point: at, context }),
      compose('closure', '这里有围挡，或路走不通', '围挡 / 封路', { point: at, context }),
      compose('gray_area', '这一片缺设施', '灰色区域', { vertices: [at], context }),
    ],
  }
}

function build(ctx: MapContext, props: Props): Content {
  switch (ctx.kind) {
    case 'cell':
      return cellContent(ctx.cell, ctx.missing, props.places, props.limitM, props.canMark)
    case 'region':
      return regionContent(ctx.region, props.canMark)
    case 'place':
      return placeContent(ctx.place, props.canMark, props.feedbackDisabled)
    case 'suspect':
      return suspectContent(ctx.suspect, props.canMark)
    case 'site': {
      const c = ctx.site
      return {
        badge: { glyph: '工', color: '#b45309' },
        title: `工地候选 · ${c.name}`,
        sub: `距中心 ${c.distance_m} 米${c.on_route ? ' · 压在一条步行路线上' : ''}`,
        body: (
          <p className="map-pop-note">
            {c.address ? `${c.address}。` : ''}这是 POI 登记数据，需要到现场确认是否立了围挡。
          </p>
        ),
        ask: '现场确实被挡住了吗？',
        actions: clueActions(
          `poi-${c.lat}-${c.lng}`,
          c,
          c.radius_m,
          `工地：${c.name}`.slice(0, 40),
          'poi',
          props.canMark,
        ),
      }
    }
    case 'point':
      return pointContent(ctx.lat, ctx.lng)
  }
}

/**
 * 地图上的就地卡片：点方格、设施、疑似点、灰色区域编号或空白处时弹出，
 * 说明这里的判定，并给出能做的标注——类型与位置自动带好，一到两步完成。
 * 它是 React 组件而不是百度的信息窗：信息窗只收 HTML 字符串，按钮绑不上事件。
 */
export function MapPopover(props: Props) {
  const { ctx, x, y, shellW, shellH, onIntent, onClose } = props
  const cardRef = useRef<HTMLDivElement>(null)
  const [height, setHeight] = useState(0)

  useEffect(() => {
    const el = cardRef.current
    if (!el) return
    el.focus({ preventScroll: true })
    const ro = new ResizeObserver(() => setHeight(el.offsetHeight))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const base = build(ctx, props)
  const trips: Action[] = !props.canTrip ? [] : ctx.kind === 'place' ? [{
    key: 'trip-to', icon: 'route', color: '#1c3a28', soft: '#ecfdf3', label: '从起点走过去', tag: '出行', intent: { kind: 'trip-to', place: ctx.place }
  }] : ctx.kind === 'cell' ? ctx.missing.map(category => ({
    key: `trip-${category}`, icon: 'route' as const, color: PLACE_MARK[category]?.color ?? '#1c3a28', soft: '#ecfdf3',
    label: `最近的${short(category)}怎么走`, tag: '出行', intent: { kind: 'trip-from-cell' as const, cell: ctx.cell, category }
  })) : []
  const content = { ...base, actions: [...trips, ...base.actions] }
  // 右边放得下就放右边，否则放左边；上下夹在地图里，箭头始终指着锚点
  const right = x + GAP + WIDTH <= shellW - MARGIN || x - GAP - WIDTH < MARGIN
  const left = Math.max(MARGIN, right ? x + GAP : x - GAP - WIDTH)
  const h = height || 220
  const top = Math.max(MARGIN, Math.min(y - 40, shellH - MARGIN - h))
  const arrow = Math.max(16, Math.min(y - top, h - 16))

  return (
    <div
      ref={cardRef}
      className={right ? 'map-pop at-right' : 'map-pop at-left'}
      role="dialog"
      aria-label={content.title}
      tabIndex={-1}
      style={{ left, top, width: WIDTH, '--arrow-y': `${arrow}px` } as CSSProperties}
    >
      <header className="map-pop-head">
        {content.badge && (
          <i className="map-pop-badge" style={{ background: content.badge.color }}>
            {content.badge.glyph}
          </i>
        )}
        <div>
          <h3>{content.title}</h3>
          {content.sub && <p>{content.sub}</p>}
        </div>
        <button type="button" className="map-pop-close" aria-label="关闭" onClick={onClose}>
          <Icon name="close" size={14} />
        </button>
      </header>
      {content.body}
      {content.actions.length > 0 && (
        <>
          {content.ask && <p className="map-pop-ask">{content.ask}</p>}
          <ul className="map-pop-actions">
            {content.actions.map((a) => (
              <li key={a.key}>
                <button
                  type="button"
                  style={{ '--act': a.color, '--act-soft': a.soft } as CSSProperties}
                  onClick={() => {
                    onIntent(a.intent)
                    onClose()
                  }}
                >
                  <i>
                    <Icon name={a.icon} size={15} />
                  </i>
                  <span>{a.label}</span>
                  {a.tag && <em>{a.tag}</em>}
                  <Icon name="chevron" size={12} />
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {content.foot && <p className="map-pop-foot">{content.foot}</p>}
    </div>
  )
}
