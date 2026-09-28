/**
 * KB store 单元测试（W6 #63；锁定 #34 切换知识库清空文档选择的行为）。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useKBStore } from '@/stores/kb'
import type { KnowledgeBase } from '@/queries/kb'

function makeKB(id: string): KnowledgeBase {
  return { id, name: `KB-${id}`, created_at: '', updated_at: '' }
}

describe('useKBStore', () => {
  beforeEach(() => {
    // setup 式 store 无 $reset：每个用例新建 Pinia 实例实现状态隔离
    setActivePinia(createPinia())
  })

  it('selectDocument 切换选中与取消', () => {
    const store = useKBStore()
    store.selectDocument({ id: 'd1' })
    expect(store.isSelected({ id: 'd1' })).toBe(true)
    store.selectDocument({ id: 'd1' })
    expect(store.isSelected({ id: 'd1' })).toBe(false)
  })

  it('selectOneDocuments 全选后 selectedCount 正确', () => {
    const store = useKBStore()
    store.selectAllDocuments([{ id: 'a' }, { id: 'b' }, { id: 'c' }])
    expect(store.selectedCount).toBe(3)
    store.selectAllDocuments(null)
    expect(store.selectedCount).toBe(0)
  })

  it('setCurrentKB 切换目标时清空文档选择（W6 #34）', () => {
    const store = useKBStore()
    store.setCurrentKB(makeKB('kb-1'))
    store.selectDocument({ id: 'd1' })
    store.selectDocument({ id: 'd2' })
    expect(store.selectedCount).toBe(2)

    // 切到另一个知识库 → 文档选择清空
    store.setCurrentKB(makeKB('kb-2'))
    expect(store.selectedCount).toBe(0)
  })

  it('setCurrentKB 重复设置同一知识库不误清选择', () => {
    const store = useKBStore()
    store.setCurrentKB(makeKB('kb-1'))
    store.selectDocument({ id: 'd1' })

    store.setCurrentKB(makeKB('kb-1'))
    expect(store.selectedCount).toBe(1)
  })

  it('setCurrentKB(null) 取消选中并清空文档选择', () => {
    const store = useKBStore()
    store.setCurrentKB(makeKB('kb-1'))
    store.selectDocument({ id: 'd1' })

    store.setCurrentKB(null)
    expect(store.currentKB).toBeNull()
    expect(store.selectedCount).toBe(0)
  })

  it('toggleBatchKBMode 退出时清空知识库选择', () => {
    const store = useKBStore()
    store.toggleBatchKBMode()
    store.selectKB({ id: 'kb-1' })
    store.selectKB({ id: 'kb-2' })
    expect(store.selectedKBCount).toBe(2)

    store.toggleBatchKBMode()
    expect(store.isBatchKBMode).toBe(false)
    expect(store.selectedKBCount).toBe(0)
  })
})
