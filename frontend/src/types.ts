/** 后端 /api 返回的数据结构。 */

export interface IsochroneProperties {
  minutes: number
  area_m2: number
  area_km2: number
  mean_radius_m: number
  min_radius_m: number
  max_radius_m: number
  /** 最短方向半径 ÷ 最远方向半径。明显偏低意味着存在铁路、河道等可达性切割。 */
  compactness: number
  sampled_points: number
  /** 算路失败的采样点数。非零时结果偏保守，需在界面上提示。 */
  failed_points: number
  name?: string
  center?: { lat: number; lng: number }
  generated_at?: string
}

export interface IsochroneFeature {
  type: 'Feature'
  geometry: {
    type: 'Polygon'
    /** GeoJSON 惯例为 [经度, 纬度]，与百度 API 的 (lat, lng) 顺序相反。 */
    coordinates: [number, number][][]
  }
  properties: IsochroneProperties
}

export interface SampleMeta {
  id: string
  name: string
  minutes: number
  center: { lat: number; lng: number }
  area_km2: number
  mean_radius_m: number
  compactness: number
  generated_at: string | null
}

export interface AppConfig {
  browser_ak: string
  default_minutes: number
  default_directions: number
}

export interface ApiErrorDetail {
  code: string
  message: string
  status?: number
}
