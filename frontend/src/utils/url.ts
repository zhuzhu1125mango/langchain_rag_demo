/**
 * URL 安全校验工具。
 */

/**
 * 校验 URL 是否允许在新窗口打开。
 * 仅允许 http/https scheme，防止 javascript: 等危险协议触发 XSS。
 *
 * @param url 待校验的 URL
 * @returns 是否允许打开
 */
export function isSafeExternalUrl(url: string): boolean {
  if (!url) return false
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

/**
 * 在已校验安全的前提下打开外部链接。
 * 不安全的 URL 将被静默忽略。
 *
 * @param url 待打开的 URL
 */
export function openExternalUrl(url: string): void {
  if (!isSafeExternalUrl(url)) return
  window.open(url, '_blank', 'noopener,noreferrer')
}
