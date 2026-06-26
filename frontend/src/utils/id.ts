/**
 * 唯一 ID 生成工具。
 *
 * 提供轻量级前端 ID 生成，用于消息、Toast 等临时对象的唯一标识，
 * 不保证跨进程唯一性，仅满足前端响应式 key 与临时引用需求。
 */

/** 生成唯一 ID：基于时间戳（36 进制）与随机串组合。 */
export function generateId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 11)}`
}
