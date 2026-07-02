import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { KnowledgeBase } from '@/queries/kb'

/**
 * 知识库与文档相关状态管理。
 *
 * 维护知识库列表、当前选中的知识库、文档选择状态。
 */
export const useKBStore = defineStore('kb', () => {
  // 文档批量选择状态
  const selectedDocumentIds = ref(new Set<string>())
  // 知识库批量选择状态
  const selectedKBIds = ref(new Set<string>())
  const isBatchKBMode = ref(false)
  const knowledgeBases = ref<KnowledgeBase[]>([])
  const currentKB = ref<KnowledgeBase | null>(null)

  /** 切换单个文档的选中状态。 */
  function selectDocument(doc: { id: string }) {
    if (selectedDocumentIds.value.has(doc.id)) {
      selectedDocumentIds.value.delete(doc.id)
    } else {
      selectedDocumentIds.value.add(doc.id)
    }
  }

  /** 全选/取消全选文档列表。 */
  function selectAllDocuments(docs: { id: string }[] | null) {
    if (!docs || !Array.isArray(docs)) {
      selectedDocumentIds.value.clear()
      return
    }
    selectedDocumentIds.value = new Set(docs.map(d => d.id))
  }

  /** 清空所有文档的选中状态。 */
  function clearSelection() {
    selectedDocumentIds.value.clear()
  }

  /** 判断指定文档是否处于选中状态。 */
  function isSelected(doc: { id: string }) {
    return selectedDocumentIds.value.has(doc.id)
  }

  const selectedDocuments = computed(() => {
    return Array.from(selectedDocumentIds.value)
  })

  const selectedCount = computed(() => {
    return selectedDocumentIds.value.size
  })

  /** 切换单个知识库的选中状态。 */
  function selectKB(kb: { id: string }) {
    if (selectedKBIds.value.has(kb.id)) {
      selectedKBIds.value.delete(kb.id)
    } else {
      selectedKBIds.value.add(kb.id)
    }
  }

  /** 全选/取消全选知识库列表。 */
  function selectAllKBs(kbs: { id: string }[] | null) {
    if (!kbs || !Array.isArray(kbs)) {
      selectedKBIds.value.clear()
      return
    }
    selectedKBIds.value = new Set(kbs.map(kb => kb.id))
  }

  /** 清空所有知识库的选中状态。 */
  function clearKBSelection() {
    selectedKBIds.value.clear()
  }

  /** 判断指定知识库是否处于选中状态。 */
  function isKBSelected(kb: { id: string }) {
    return selectedKBIds.value.has(kb.id)
  }

  /** 切换知识库批量管理模式。 */
  function toggleBatchKBMode() {
    isBatchKBMode.value = !isBatchKBMode.value
    if (!isBatchKBMode.value) {
      clearKBSelection()
    }
  }

  /** 退出知识库批量管理模式并清空选择。 */
  function exitBatchKBMode() {
    isBatchKBMode.value = false
    clearKBSelection()
  }

  const selectedKBs = computed(() => {
    return Array.from(selectedKBIds.value)
  })

  const selectedKBCount = computed(() => {
    return selectedKBIds.value.size
  })

  /** 设置知识库列表；未选中当前知识库时自动选择默认知识库。 */
  function setKnowledgeBases(kbs: KnowledgeBase[]) {
    knowledgeBases.value = kbs
    if (!currentKB.value && kbs.length > 0) {
      const defaultKB = kbs.find(kb => kb.is_default)
      currentKB.value = (defaultKB !== undefined ? defaultKB : kbs[0]) as KnowledgeBase
    }
  }

  /** 设置当前选中的知识库，传 null 表示取消选中。 */
  function setCurrentKB(kb: KnowledgeBase | null) {
    currentKB.value = kb
  }

  return {
    selectedDocuments,
    selectedCount,
    selectedKBIds,
    selectedKBs,
    selectedKBCount,
    isBatchKBMode,
    knowledgeBases,
    currentKB,
    selectDocument,
    selectAllDocuments,
    clearSelection,
    isSelected,
    selectKB,
    selectAllKBs,
    clearKBSelection,
    isKBSelected,
    toggleBatchKBMode,
    exitBatchKBMode,
    setKnowledgeBases,
    setCurrentKB
  }
})
