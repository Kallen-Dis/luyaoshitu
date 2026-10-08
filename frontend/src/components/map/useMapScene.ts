import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

/**
 * 地图的「看法」：3D 倾斜与旋转、卫星底图、全屏。
 *
 * 全部只动浏览器端的 JS API GL，不调任何服务端接口，不花配额。
 * 用到的 BMapGL 能力（JSAPI WebGL 1.0 类参考）：setTilt / setHeading、tilt_changed /
 * heading_changed 事件、setMapType(BMAP_EARTH_MAP)。旧版本或个别浏览器缺哪项就把对应按钮禁用。
 *
 * 试过又拿掉的两项：实时路况图层（百度路况只画机动车道路，街区尺度下几乎看不到，
 * 也和步行判定无关）；环绕动画（视角动画在 0°/360° 交界处会倒转、和手动拖动互相打架）。
 */

/** 进入 3D 时的倾斜角。再大远处的方格会挤成一条线，读不出格子。 */
const TILT_3D = 55
/** 小于这个角度视为「正北朝上、没有倾斜」 */
const FLAT_EPS = 0.5

const NO_SUPPORT = { tilt: false, earth: false, fullscreen: false }

export interface MapScene {
  /** 倾斜角（度） */
  tilt: number
  /** 朝向（度，-180 ~ 180，0 为正北朝上） */
  heading: number
  /** 有倾斜或旋转：画布叠加的热力色场对不上，要改用矢量方格 */
  angled: boolean
  earth: boolean
  fullscreen: boolean
  support: { tilt: boolean; earth: boolean; fullscreen: boolean }
  toggle3d: () => void
  rotate: (delta: number) => void
  resetNorth: () => void
  toggleEarth: () => void
  setEarth: (value: boolean) => void
  toggleFullscreen: () => void
}

function normHeading(h: number): number {
  const x = (((h % 360) + 540) % 360) - 180
  return Math.abs(x) < 1e-6 ? 0 : x
}

export function useMapScene(
  mapRef: RefObject<any>,
  ready: boolean,
  /** 全屏时放大哪个元素：默认是地图外壳，传入舞台可以把搜索框、关键读数一起带上 */
  fullscreenRoot: () => HTMLElement | null,
): MapScene {
  const [tilt, setTilt] = useState(0)
  const [heading, setHeading] = useState(0)
  const [earth, setEarth] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [support, setSupport] = useState<MapScene['support']>(NO_SUPPORT)
  const rootRef = useRef(fullscreenRoot)
  useEffect(() => {
    rootRef.current = fullscreenRoot
  })

  // 地图建好之后检测一次这个版本支持哪些能力（缺的就把按钮禁用）
  useEffect(() => {
    const m = mapRef.current
    if (!ready || !m) return
    const g = window as unknown as Record<string, unknown>
    setSupport({
      tilt: typeof m.setTilt === 'function' && typeof m.setHeading === 'function',
      earth: typeof m.setMapType === 'function' && g.BMAP_EARTH_MAP !== undefined,
      fullscreen: Boolean(document.fullscreenEnabled),
    })
  }, [ready, mapRef])

  // 视角随用户拖动（右键 / Ctrl 拖动可旋转倾斜）而变：读回地图的真实角度
  useEffect(() => {
    const m = mapRef.current
    if (!ready || !m || typeof m.getTilt !== 'function') return
    let frame = 0
    const sync = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        setTilt(Number(m.getTilt()) || 0)
        setHeading(normHeading(Number(m.getHeading?.()) || 0))
      })
    }
    sync()
    // tilt_changed / heading_changed 是官方事件；再挂几个视图结束事件兜底，个别版本前者不触发
    const events = ['tilt_changed', 'heading_changed', 'moveend', 'zoomend', 'dragend']
    events.forEach((e) => m.addEventListener(e, sync))
    return () => {
      cancelAnimationFrame(frame)
      events.forEach((e) => m.removeEventListener(e, sync))
    }
  }, [ready, mapRef])

  useEffect(() => {
    const onChange = () => {
      const root = rootRef.current()
      setFullscreen(document.fullscreenElement !== null && document.fullscreenElement === root)
    }
    document.addEventListener('fullscreenchange', onChange)
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [])

  const maxTilt = () => {
    const m = mapRef.current
    const cap = typeof m?.getCurrentMaxTilt === 'function' ? Number(m.getCurrentMaxTilt()) : TILT_3D
    return Math.min(TILT_3D, Number.isFinite(cap) && cap > 0 ? cap : TILT_3D)
  }

  const angled = tilt > FLAT_EPS || Math.abs(heading) > FLAT_EPS

  const toggle3d = () => {
    const m = mapRef.current
    if (!m || !support.tilt) return
    if (angled) {
      m.setTilt(0)
      m.setHeading(0)
    } else {
      m.setTilt(maxTilt())
    }
  }

  const rotate = (delta: number) => {
    const m = mapRef.current
    if (!m || !support.tilt) return
    m.setHeading(Number(m.getHeading?.() ?? 0) + delta)
  }

  const resetNorth = () => {
    const m = mapRef.current
    if (!m || !support.tilt) return
    m.setHeading(0)
  }

  const setEarthMode = useCallback((value: boolean) => {
    const m = mapRef.current
    if (!m || typeof m.setMapType !== 'function') return
    const w = window as unknown as Record<string, unknown>
    const mapType = value ? w.BMAP_EARTH_MAP : w.BMAP_NORMAL_MAP
    if (mapType === undefined) return
    m.setMapType(mapType)
    setEarth(value)
  }, [mapRef])
  const toggleEarth = () => setEarthMode(!earth)

  const toggleFullscreen = () => {
    if (!support.fullscreen) return
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => undefined)
      return
    }
    const root = rootRef.current()
    if (root) void root.requestFullscreen().catch(() => undefined)
  }

  return {
    tilt,
    heading,
    angled,
    earth,
    fullscreen,
    support,
    toggle3d,
    rotate,
    resetNorth,
    toggleEarth,
    setEarth: setEarthMode,
    toggleFullscreen,
  }
}
