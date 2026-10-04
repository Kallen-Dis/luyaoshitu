/**
 * 百度地图 JS API GL 的按需加载器。
 *
 * AK 由后端 /api/config 下发而非写死在源码里，因此脚本只能在运行时注入。
 * 加载走官方的 callback 参数，比轮询 window.BMapGL 更可靠。
 */

declare global {
  interface Window {
    BMapGL?: any
    __onBMapGLReady?: () => void
  }
}

const CALLBACK_NAME = '__onBMapGLReady'
let loading: Promise<void> | null = null

export function loadBaiduMap(ak: string): Promise<void> {
  if (window.BMapGL) return Promise.resolve()
  // 多个组件可能同时触发加载，共享同一个 Promise 避免重复注入脚本
  if (loading) return loading

  loading = new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      // 清掉共享的 Promise，否则之后每次调用都拿到这个已失败的结果，页面刷新前再也加载不了
      loading = null
      reject(
        new Error(
          '百度地图脚本加载超时。请确认网络可达，以及该 AK 的 Referer 白名单已包含 localhost/*',
        ),
      )
    }, 20000)

    window[CALLBACK_NAME] = () => {
      window.clearTimeout(timer)
      resolve()
    }

    const script = document.createElement('script')
    script.src = `https://api.map.baidu.com/api?type=webgl&v=1.0&ak=${encodeURIComponent(
      ak,
    )}&callback=${CALLBACK_NAME}`
    script.async = true
    script.onerror = () => {
      window.clearTimeout(timer)
      loading = null
      reject(new Error('百度地图脚本加载失败，请检查网络与 AK 配置'))
    }
    document.head.appendChild(script)
  })

  return loading
}
