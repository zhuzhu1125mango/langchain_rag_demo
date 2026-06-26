/**
 * WebSocket 地址构造工具。
 *
 * 开发环境走 localhost:8000，生产环境走当前页面 host 的 ws/wss 协议。
 */

/** 根据环境构造 WebSocket 完整地址。path 需以 / 开头，可包含查询字符串。 */
export function buildWsUrl(path: string): string {
  if (import.meta.env.DEV) {
    return `ws://localhost:8000${path}`
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}${path}`
}
