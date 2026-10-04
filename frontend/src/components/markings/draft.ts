import { useCallback, useReducer } from 'react'
import { metersBetween, type LatLng } from '../../lib/markings'
import type { MarkingType, Place } from '../../types'

/** 正在画的标注草图。地图负责画它，编辑面板负责改它。 */
export interface Draft {
  type: MarkingType
  /** 围挡圆心 / 设施位置 */
  point: LatLng | null
  /** 围挡半径（米） */
  radius: number
  /** 灰色区域的顶点，按点击顺序 */
  vertices: LatLng[]
  /** 设施失效：选中的地图设施 */
  place: Place | null
}

export function emptyDraft(type: MarkingType, radius = 50): Draft {
  return { type, point: null, radius, vertices: [], place: null }
}

interface HistoryState {
  past: Draft[]
  present: Draft
  future: Draft[]
}

type Action =
  | { kind: 'push'; draft: Draft }
  | { kind: 'undo' }
  | { kind: 'redo' }
  | { kind: 'reset'; draft: Draft }

const LIMIT = 100

function reducer(state: HistoryState, action: Action): HistoryState {
  switch (action.kind) {
    case 'push':
      return {
        past: [...state.past, state.present].slice(-LIMIT),
        present: action.draft,
        future: [],
      }
    case 'undo': {
      const prev = state.past[state.past.length - 1]
      if (!prev) return state
      return { past: state.past.slice(0, -1), present: prev, future: [state.present, ...state.future] }
    }
    case 'redo': {
      const [next, ...rest] = state.future
      if (!next) return state
      return { past: [...state.past, state.present], present: next, future: rest }
    }
    case 'reset':
      return { past: [], present: action.draft, future: [] }
  }
}

export interface DraftHistory {
  draft: Draft
  push: (draft: Draft) => void
  undo: () => void
  redo: () => void
  reset: (draft: Draft) => void
  canUndo: boolean
  canRedo: boolean
}

/** 草图的撤销 / 重做栈。只在本地，不经过服务器。 */
export function useDraftHistory(initial: Draft): DraftHistory {
  const [state, dispatch] = useReducer(reducer, { past: [], present: initial, future: [] })
  const push = useCallback((draft: Draft) => dispatch({ kind: 'push', draft }), [])
  const undo = useCallback(() => dispatch({ kind: 'undo' }), [])
  const redo = useCallback(() => dispatch({ kind: 'redo' }), [])
  const reset = useCallback((draft: Draft) => dispatch({ kind: 'reset', draft }), [])
  return {
    draft: state.present,
    push,
    undo,
    redo,
    reset,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
  }
}

/** 点击地图时草图怎么变。设施失效：吸附到 80 米内最近的地图设施。 */
export function applyMapClick(
  draft: Draft,
  at: LatLng,
  places: Place[],
  maxVertices: number,
): Draft {
  switch (draft.type) {
    case 'closure':
    case 'facility_extra':
      return { ...draft, point: at }
    case 'gray_area':
      if (draft.vertices.length >= maxVertices) return draft
      return { ...draft, vertices: [...draft.vertices, at] }
    case 'facility_missing': {
      const nearest = places
        .filter((p) => p.source !== 'user')
        .map((p) => ({ p, d: metersBetween(at, p) }))
        .sort((a, b) => a.d - b.d)[0]
      if (nearest && nearest.d <= 80) {
        return { ...draft, place: nearest.p, point: { lat: nearest.p.lat, lng: nearest.p.lng } }
      }
      return { ...draft, place: null, point: at }
    }
  }
}
