import { Icon } from '../markings/Icon'
import type { MapScene } from './useMapScene'

const UNSUPPORTED = '当前浏览器或地图版本不支持'

/**
 * 地图右下角：视角（3D、旋转、回正）、卫星底图与全屏。
 * 只改看法，不改结果，也不花配额。旋转与回正只在有倾斜或旋转时出现，平时只占三格。
 */
export function MapSceneBar({ scene }: { scene: MapScene }) {
  const { support, angled } = scene
  const turned = Math.abs(scene.heading) > 0.5

  return (
    <div className="map-scene">
      <div className="scene-group floating" role="group" aria-label="地图视角">
        <button
          type="button"
          aria-pressed={angled}
          disabled={!support.tilt}
          title={
            support.tilt
              ? angled
                ? '回到正北朝上的平面视角'
                : '倾斜成 3D 视角，放大到街区能看到楼体（也可按住右键拖动旋转、倾斜）'
              : UNSUPPORTED
          }
          onClick={scene.toggle3d}
        >
          <Icon name="cube" size={15} />
          3D
        </button>
        {angled && (
          <div className="scene-pair">
            <button
              type="button"
              aria-label="向左转 45°"
              title="向左转 45°"
              onClick={() => scene.rotate(-45)}
            >
              <Icon name="rotate-left" size={15} />
            </button>
            <button
              type="button"
              aria-label="向右转 45°"
              title="向右转 45°"
              onClick={() => scene.rotate(45)}
            >
              <Icon name="rotate-right" size={15} />
            </button>
          </div>
        )}
        {turned && (
          <button
            type="button"
            className="scene-compass"
            aria-label={`回正：现在朝向 ${Math.round(scene.heading)}°，点一下正北朝上`}
            title="正北朝上"
            onClick={scene.resetNorth}
          >
            <span style={{ transform: `rotate(${-scene.heading}deg)` }}>
              <Icon name="compass" size={15} />
            </span>
            北
          </button>
        )}
      </div>

      <div className="scene-group floating" role="group" aria-label="底图">
        <button
          type="button"
          aria-pressed={scene.earth}
          disabled={!support.earth}
          title={support.earth ? '切换卫星影像底图' : UNSUPPORTED}
          onClick={scene.toggleEarth}
        >
          <Icon name="satellite" size={15} />
          卫星
        </button>
      </div>

      <div className="scene-group floating" role="group" aria-label="全屏">
        <button
          type="button"
          aria-pressed={scene.fullscreen}
          disabled={!support.fullscreen}
          title={
            support.fullscreen
              ? scene.fullscreen
                ? '退出全屏（Esc）'
                : '地图全屏，适合答辩演示与录屏'
              : UNSUPPORTED
          }
          onClick={scene.toggleFullscreen}
        >
          <Icon name={scene.fullscreen ? 'collapse' : 'expand'} size={15} />
          {scene.fullscreen ? '退出' : '全屏'}
        </button>
      </div>
    </div>
  )
}
