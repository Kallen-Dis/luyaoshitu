import { useEffect, useRef } from 'react'
import { loadBaiduMap } from '../baiduMap'
import type { IsochroneFeature } from '../types'

interface Props {
  ak: string
  center: { lat: number; lng: number }
  isochrone: IsochroneFeature | null
  onPickCenter: (lat: number, lng: number) => void
  onError: (message: string) => void
}

/**
 * 地图视图：渲染等时圈多边形、中心点标注，并支持点击改选中心点。
 *
 * 百度地图实例通过 ref 持有而非放进 state——它是命令式的可变对象，
 * 放进 state 会触发无意义的重渲染，还可能导致地图被反复销毁重建。
 */
export function MapView({ ak, center, isochrone, onPickCenter, onError }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<any>(null)
  const overlaysRef = useRef<any[]>([])
  // 点击回调里要用到最新的处理函数，但地图监听只注册一次，故用 ref 转发
  const pickRef = useRef(onPickCenter)
  pickRef.current = onPickCenter

  useEffect(() => {
    let cancelled = false

    loadBaiduMap(ak)
      .then(() => {
        if (cancelled || !containerRef.current || mapRef.current) return
        const BMapGL = window.BMapGL
        const map = new BMapGL.Map(containerRef.current)
        map.centerAndZoom(new BMapGL.Point(center.lng, center.lat), 15)
        map.enableScrollWheelZoom(true)
        map.addControl(new BMapGL.ScaleControl())
        map.addControl(new BMapGL.ZoomControl())
        map.addEventListener('click', (e: any) => {
          if (e.latlng) pickRef.current(e.latlng.lat, e.latlng.lng)
        })
        mapRef.current = map
      })
      .catch((err: Error) => {
        if (!cancelled) onError(err.message)
      })

    return () => {
      cancelled = true
    }
    // 仅在 AK 变化时重建地图；中心点变化由下面的 effect 单独处理
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ak])

  // 绘制等时圈与中心点
  useEffect(() => {
    const map = mapRef.current
    const BMapGL = window.BMapGL
    if (!map || !BMapGL) return

    overlaysRef.current.forEach((o) => map.removeOverlay(o))
    overlaysRef.current = []

    const marker = new BMapGL.Marker(new BMapGL.Point(center.lng, center.lat))
    map.addOverlay(marker)
    overlaysRef.current.push(marker)

    if (isochrone) {
      const ring = isochrone.geometry.coordinates[0] ?? []
      const points = ring.map(([lng, lat]) => new BMapGL.Point(lng, lat))
      const polygon = new BMapGL.Polygon(points, {
        strokeColor: '#1f6feb',
        strokeWeight: 2,
        strokeOpacity: 0.9,
        fillColor: '#1f6feb',
        fillOpacity: 0.18,
      })
      map.addOverlay(polygon)
      overlaysRef.current.push(polygon)
      // 视野贴合等时圈范围，比固定缩放级别更实用——
      // 不同社区的可达范围差异很大，桃浦镇比曹杨新村小了近三成
      map.setViewport(points)
    } else {
      map.setCenter(new BMapGL.Point(center.lng, center.lat))
    }
  }, [center, isochrone])

  return <div ref={containerRef} className="map-canvas" />
}
