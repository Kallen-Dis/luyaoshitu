export const NOTICE_DURATION_MS = 8000

/** 取消后忽略已排队的回调，避免退出或切换提示后写入旧状态。 */
export function scheduleNoticeDismiss(
  dismiss: () => void,
  schedule: (callback: () => void, delay: number) => ReturnType<typeof setTimeout> = setTimeout,
  cancel: (timer: ReturnType<typeof setTimeout>) => void = clearTimeout,
): () => void {
  let active = true
  const timer = schedule(() => { if (active) dismiss() }, NOTICE_DURATION_MS)
  return () => { active = false; cancel(timer) }
}
