/**
 * Toast 通知组合式函数，提供全局消息提示能力。
 *
 * 维护一份全局 Toast 列表，支持 success/error/warning/info 四种类型，
 * 并通过 ToastNotification 组件统一渲染，超时自动移除。
 */
import { ref } from 'vue'
import { generateId } from '@/utils/id'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface Toast {
  /** 唯一标识，用于定位与移除。 */
  id: string
  /** Toast 类型，决定图标与配色。 */
  type: ToastType
  /** 标题文本。 */
  title: string
  /** 可选的详细描述。 */
  message?: string
  /** 展示时长（毫秒），未设置时默认 4000ms。 */
  duration?: number
}

/** 全局 Toast 列表，供 ToastNotification 组件统一渲染。 */
const toasts = ref<Toast[]>([])

/** 按 ID 移除指定的 Toast（手动关闭或自动超时调用）。 */
function removeToast(id: string): void {
  const index = toasts.value.findIndex(t => t.id === id)
  if (index !== -1) {
    toasts.value.splice(index, 1)
  }
}

/** 添加一条 Toast，默认 4 秒后自动移除。 */
function addToast(toast: Omit<Toast, 'id'>): void {
  const id = generateId()
  const newToast: Toast = { ...toast, id, duration: toast.duration ?? 4000 }
  toasts.value.push(newToast)

  setTimeout(() => {
    removeToast(id)
  }, newToast.duration)
}

/** Toast 提示 Composable，提供 success/error/warning/info 四种快捷调用。 */
export function useToast() {
  function success(title: string, message?: string): void {
    addToast({ type: 'success', title, message })
  }

  function error(title: string, message?: string): void {
    addToast({ type: 'error', title, message })
  }

  function warning(title: string, message?: string): void {
    addToast({ type: 'warning', title, message })
  }

  function info(title: string, message?: string): void {
    addToast({ type: 'info', title, message })
  }

  return {
    toasts,
    success,
    error,
    warning,
    info,
    removeToast
  }
}