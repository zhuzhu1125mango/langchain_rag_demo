import { api } from '@/utils/axios'

/**
 * 下载文档原文（P1-2）。
 *
 * 为什么要走 XHR 而不是 `<a href="/api/documents/{id}/file">`：
 * 该接口需要 `Authorization` 头，浏览器直接点链接不会带上令牌，
 * 只会得到一个 401 页面。所以先用 axios 取 blob，再用临时对象 URL 触发保存。
 *
 * 文件名用调用方传入的（来自文档元数据）而不是解析响应头：
 * `Content-Disposition` 在跨域/代理改写后不一定还在，而 DB 里的名字才是权威。
 */
export async function downloadDocumentFile(docId: string, filename: string): Promise<void> {
  const blob = (await api.get(`/documents/${docId}/file`, {
    responseType: 'blob',
  })) as unknown as Blob

  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename || 'document'
  link.rel = 'noopener'
  document.body.appendChild(link)
  link.click()
  link.remove()

  // 立即 revoke 会让部分浏览器（Safari/部分 WebView）取不到内容，
  // 10s 后回收既够下载启动，也不会长期占内存
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

/**
 * 从失败响应里取出可读信息。
 *
 * `responseType: 'blob'` 时错误体也是 Blob，直接 `String(err)` 只能得到
 * `[object Blob]`；这里读回文本并尝试按 JSON 解析 `detail`。
 */
export async function extractDownloadError(error: unknown): Promise<string> {
  const data = (error as { response?: { data?: unknown } })?.response?.data
  if (data instanceof Blob) {
    try {
      const text = await data.text()
      try {
        const parsed = JSON.parse(text) as { detail?: string; message?: string }
        return parsed.detail || parsed.message || text
      } catch {
        return text
      }
    } catch {
      return '下载失败'
    }
  }
  if (error instanceof Error && error.message) return error.message
  return '下载失败'
}
