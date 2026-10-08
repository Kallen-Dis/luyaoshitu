import type { ReactNode } from 'react'
import type { MarkingType } from '../../types'

export type IconName =
  | MarkingType
  | 'plus'
  | 'camera'
  | 'check'
  | 'alert'
  | 'undo'
  | 'redo'
  | 'trash'
  | 'close'
  | 'pin'
  | 'shield'
  | 'clock'
  | 'back'
  | 'chevron'
  | 'eye'
  | 'eye-off'
  | 'edit'
  | 'history'
  | 'layers'
  | 'cube'
  | 'rotate-left'
  | 'rotate-right'
  | 'compass'
  | 'satellite'
  | 'expand'
  | 'collapse'
  | 'route'
  | 'flag'
  | 'walk'

/** 统一 1.6 描边的小图标。aria-hidden：图标旁边总有文字，读屏只读文字。 */
export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  )
}

const PATHS: Record<IconName, ReactNode> = {
  route: <><circle cx="5" cy="18" r="2" /><circle cx="19" cy="6" r="2" /><path d="M7 18h8a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h8" /></>,
  flag: <><path d="M5 21V3l14 2-4 5 4 5-14-2" /></>,
  walk: <><circle cx="14" cy="4" r="2" /><path d="m9 21 3-8 3 8M5 13l5-6 5 4 4 1M10 7l2 6" /></>,
  // 围挡：两根立柱夹着斜条纹
  closure: (
    <>
      <path d="M4 20V6M20 20V6" />
      <rect x="4" y="8" width="16" height="6" rx="1" />
      <path d="M7 14l4-6M12 14l4-6M17 14l3-4.5" />
    </>
  ),
  // 设施失效：店面 + 斜杠
  facility_missing: (
    <>
      <path d="M4 10h16l-1.5-5h-13z" />
      <path d="M5 10v9h14v-9" />
      <path d="M3 3l18 18" />
    </>
  ),
  // 补录设施：店面 + 加号
  facility_extra: (
    <>
      <path d="M4 10h16l-1.5-5h-13z" />
      <path d="M5 10v9h8" />
      <path d="M19 10v3" />
      <path d="M18 16v6M15 19h6" />
    </>
  ),
  // 灰色区域：多边形 + 顶点
  gray_area: (
    <>
      <path d="M5 7l8-3 6 6-3 9-10-2z" />
      <circle cx="5" cy="7" r="1.2" fill="currentColor" />
      <circle cx="13" cy="4" r="1.2" fill="currentColor" />
      <circle cx="19" cy="10" r="1.2" fill="currentColor" />
      <circle cx="16" cy="19" r="1.2" fill="currentColor" />
      <circle cx="6" cy="17" r="1.2" fill="currentColor" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  // 图层：三张叠起来的薄片
  layers: (
    <>
      <path d="M12 4l9 4.5-9 4.5-9-4.5z" />
      <path d="M3 12.5l9 4.5 9-4.5" />
      <path d="M3 16.5l9 4.5 9-4.5" />
    </>
  ),
  camera: (
    <>
      <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
      <circle cx="12" cy="13" r="3.5" />
    </>
  ),
  check: <path d="M5 12.5l4.5 4.5L19 7" />,
  alert: (
    <>
      <path d="M12 3l10 18H2z" />
      <path d="M12 10v5M12 18v.01" />
    </>
  ),
  undo: (
    <>
      <path d="M9 14L4 9l5-5" />
      <path d="M4 9h11a5 5 0 010 10h-3" />
    </>
  ),
  redo: (
    <>
      <path d="M15 14l5-5-5-5" />
      <path d="M20 9H9a5 5 0 000 10h3" />
    </>
  ),
  trash: (
    <>
      <path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13" />
    </>
  ),
  close: <path d="M6 6l12 12M18 6L6 18" />,
  pin: (
    <>
      <path d="M12 21s-7-6.5-7-12a7 7 0 0114 0c0 5.5-7 12-7 12z" />
      <circle cx="12" cy="9" r="2.5" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" />
      <path d="M8.5 12l2.5 2.5L16 9.5" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="9" />
      <path d="M12 7v5l3 2" />
    </>
  ),
  back: <path d="M15 5l-7 7 7 7" />,
  chevron: <path d="M9 5l7 7-7 7" />,
  eye: (
    <>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  'eye-off': (
    <>
      <path d="M3 3l18 18" />
      <path d="M10.6 5.1A10 10 0 0112 5c6.5 0 10 7 10 7a17 17 0 01-3.2 4.2M6.6 6.6C3.9 8.3 2 12 2 12s3.5 7 10 7a9.6 9.6 0 004.4-1" />
      <path d="M9.9 9.9a3 3 0 004.2 4.2" />
    </>
  ),
  edit: (
    <>
      <path d="M4 20h4L19 9l-4-4L4 16z" />
      <path d="M13.5 6.5l4 4" />
    </>
  ),
  history: (
    <>
      <path d="M3 12a9 9 0 103-6.7" />
      <path d="M3 4v4h4" />
      <path d="M12 8v4l3 2" />
    </>
  ),
  // 3D：立方体
  cube: (
    <>
      <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" />
      <path d="M4 7.5l8 4.5 8-4.5M12 12v9" />
    </>
  ),
  'rotate-left': (
    <>
      <path d="M4 5v5h5" />
      <path d="M5.5 15a7 7 0 101.6-7.4L4 10" />
    </>
  ),
  'rotate-right': (
    <>
      <path d="M20 5v5h-5" />
      <path d="M18.5 15a7 7 0 11-1.6-7.4L20 10" />
    </>
  ),
  // 指北针：上半实心指北，下半空心
  compass: (
    <>
      <path d="M12 3l4 9h-8z" fill="currentColor" />
      <path d="M8 12l4 9 4-9" />
    </>
  ),
  // 卫星图：两片太阳能板夹着本体
  satellite: (
    <>
      <rect x="9.5" y="9.5" width="5" height="5" rx="1" transform="rotate(45 12 12)" />
      <path d="M8 8L4.5 4.5M16 16l3.5 3.5" />
      <path d="M2.5 6.5l4-4 3 3-4 4zM14.5 17.5l3-3 4 4-3 3z" />
    </>
  ),
  expand: <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />,
  collapse: <path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5" />,
}

/** 类型徽标：彩色方块里放图标，列表与详情共用。 */
export function TypeBadge({
  type,
  color,
  soft,
  size = 34,
}: {
  type: MarkingType
  color: string
  soft: string
  size?: number
}) {
  return (
    <span
      className="mk-type-badge"
      style={{ color, background: soft, borderColor: `${color}55`, width: size, height: size }}
    >
      <Icon name={type} size={Math.round(size * 0.55)} />
    </span>
  )
}
