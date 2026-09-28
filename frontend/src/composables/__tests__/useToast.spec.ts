/**
 * useToast / handleMutationError 单元测试（W6 #63 核心逻辑覆盖补充）。
 */
import { describe, it, expect } from 'vitest'
import { useToast, handleMutationError } from '@/composables/useToast'

describe('useToast', () => {
  it('success/error/warning/info 各类型追加到全局列表', () => {
    const toast = useToast()
    const before = toast.toasts.value.length
    toast.success('成功标题', '成功描述')
    toast.error('错误标题')
    toast.warning('警告标题')
    toast.info('信息标题')

    const added = toast.toasts.value.slice(before)
    expect(added.map(t => t.type)).toEqual(['success', 'error', 'warning', 'info'])
    expect(added[0]?.message).toBe('成功描述')
    expect(added[1]?.message).toBeUndefined()
  })

  it('removeToast 按 ID 移除指定条目', () => {
    const toast = useToast()
    toast.info('待移除')
    const last = toast.toasts.value[toast.toasts.value.length - 1]
    expect(last?.title).toBe('待移除')

    toast.removeToast(last!.id)
    expect(toast.toasts.value.find(t => t.id === last!.id)).toBeUndefined()
  })
})

describe('handleMutationError（W6 #35 统一错误提示）', () => {
  it('Error 实例取其 message（含 AxiosError）', () => {
    const toast = useToast()
    const before = toast.toasts.value.length
    handleMutationError(new Error('boom'), '操作失败')
    const added = toast.toasts.value[before]
    expect(added?.type).toBe('error')
    expect(added?.title).toBe('操作失败')
    expect(added?.message).toBe('boom')
  })

  it('非 Error 值使用兜底文案', () => {
    const toast = useToast()
    const before = toast.toasts.value.length
    handleMutationError('string error', '操作失败', '兜底文案')
    const added = toast.toasts.value[before]
    expect(added?.message).toBe('兜底文案')
  })

  it('非 Error 值缺省兜底为未知错误', () => {
    const toast = useToast()
    const before = toast.toasts.value.length
    handleMutationError(undefined, '操作失败')
    const added = toast.toasts.value[before]
    expect(added?.message).toBe('未知错误')
  })
})
