/**
 * 通用日期/时间格式化工具。
 *
 * 历史上多个视图各自重复定义了 formatDate，实现略有差异。此处收敛为统一版本，
 * 并保留 ChatView 所需的"今天仅显示时间"的特殊版本。
 */

/**
 * 将日期字符串格式化为 `YYYY/MM/DD HH:MM` 形式。
 * 入参为空（undefined/null/空串）时返回空字符串。
 */
export function formatDate(dateString?: string | null): string {
  if (!dateString) return ''
  const date = new Date(dateString)
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

/**
 * 聊天会话列表专用：若为今天则仅返回 `HH:MM`，否则返回短日期 `M月D日`。
 * 入参为空时返回空字符串。
 */
export function formatChatDate(dateString?: string | null): string {
  if (!dateString) return ''
  const date = new Date(dateString)
  const now = new Date()
  const isToday = date.toDateString() === now.toDateString()
  if (isToday) {
    return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' })
}
