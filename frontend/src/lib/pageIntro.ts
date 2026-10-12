const SEEN_KEY = 'luyaoshitu.page-intro.v1'

export function shouldOfferPageIntro(): boolean {
  try { return localStorage.getItem(SEEN_KEY) !== 'dismissed' }
  catch { return true }
}

export function rememberPageIntro(): void {
  // 禁用本地存储时也能关闭介绍，不阻断正常使用。
  try { localStorage.setItem(SEEN_KEY, 'dismissed') }
  catch { /* 本次关闭仍由 App 状态记录。 */ }
}
