import { ref } from 'vue'
import { useKBStore } from '@/stores/kb'
import { useSearchDocuments } from '@/queries/kb'
import type { SearchResult } from '@/queries/kb'
import { useToast } from '@/composables/useToast'
import type { Ref } from 'vue'

/**
 * 全文搜索编排（从 KnowledgeBaseView.vue 抽取，行为不变）。
 *
 * 覆盖：全文搜索执行、结果与加载状态、清空搜索。
 */
export function useContentSearch(currentKBId: Ref<string | undefined>) {
  const kbStore = useKBStore()
  const toast = useToast()
  const searchDocuments = useSearchDocuments()

  const searchResults = ref<SearchResult[]>([])
  const isSearching = ref(false)
  const showSearchResults = ref(false)

  /** 执行全文搜索并展示结果。 */
  async function performSearch(searchQuery: string, searchMode: 'filename' | 'content'): Promise<void> {
    if (!searchQuery.trim() || searchMode !== 'content') {
      return
    }

    isSearching.value = true
    showSearchResults.value = true

    try {
      const results = await searchDocuments(searchQuery.trim(), currentKBId.value)
      searchResults.value = results
      if (results.length === 0) {
        toast.info('未找到匹配的文档内容')
      }
    } catch (error) {
      toast.error('搜索失败', error instanceof Error ? error.message : '未知错误')
      searchResults.value = []
    } finally {
      isSearching.value = false
    }
  }

  /** 清空全文搜索状态并返回文档列表。 */
  function clearSearch(): void {
    searchResults.value = []
    showSearchResults.value = false
  }

  return {
    searchResults,
    isSearching,
    showSearchResults,
    performSearch,
    clearSearch
  }
}
