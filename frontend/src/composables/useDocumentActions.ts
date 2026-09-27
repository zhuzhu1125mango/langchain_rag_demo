import { computed } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useKBStore } from '@/stores/kb'
import { api } from '@/utils/axios'
import { useDeleteDocument, useBatchDeleteDocuments, useUpdateDocument } from '@/queries/kb'
import type { Document } from '@/queries/kb'
import { useToast } from '@/composables/useToast'
import type { Ref } from 'vue'

/**
 * 文档操作编排（从 KnowledgeBaseView.vue 抽取，行为不变）。
 *
 * 覆盖：单文档删除（乐观失效刷新）、批量删除、重新处理、上下架切换、
 * 列表全选/取消全选。
 */
export function useDocumentActions(documentsData: Ref<{ items?: Document[] } | null | undefined>) {
  const kbStore = useKBStore()
  const queryClient = useQueryClient()
  const toast = useToast()

  const deleteMutation = useDeleteDocument()
  const batchDeleteMutation = useBatchDeleteDocuments()
  const updateMutation = useUpdateDocument()

  const selectedCount = computed(() => kbStore.selectedCount)

  /** 文档列表全选/取消全选。 */
  function toggleSelectAll(event: Event): void {
    const target = event.target as HTMLInputElement
    const data = documentsData.value

    if (target.checked && data?.items?.length) {
      kbStore.selectAllDocuments(data.items)
    } else {
      kbStore.clearSelection()
    }
  }

  /**
   * 删除单个文档，采用乐观更新策略。
   *
   * 整体流程：1) 二次确认后立即从 queryClient 缓存中移除该文档（乐观更新），
   * 让 UI 即时反馈；2) 调用 deleteMutation 异步删除；3) 若 API 失败则
   * 通过 invalidateQueries 刷新文档与知识库列表以回滚到真实状态。
   */
  function deleteDocument(doc: Document): void {
    if (confirm(`确定要删除文档 "${doc.filename}" 吗？`)) {
      // 立即提示用户
      toast.info('正在删除文档...', `正在后台删除 ${doc.filename}`)

      // 立即重拉文档列表（D2）：实际 query key 为 ['documents', effectiveParams]
      // （queries/kb.ts），不能写死 ['documents'] 做乐观 setQueryData——
      // 此前写入无人读取的键导致删除不生效。失效命中前缀即可触发精确重拉（既有惯例）。
      queryClient.invalidateQueries({ queryKey: ['documents'] })

      // 调用API删除
      deleteMutation.mutate(doc.id, {
        onSuccess: () => {
          // API已返回，但后端是异步删除
          console.log('[Delete] Delete task submitted')
        },
        onError: (error) => {
          // 如果API失败，恢复文档列表
          toast.error('删除失败', error instanceof Error ? error.message : '未知错误')
          queryClient.invalidateQueries({ queryKey: ['documents'] })
          queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
        }
      })
    }
  }

  /** 提交文档重新处理任务。 */
  async function reprocessDocument(doc: Document): Promise<void> {
    if (confirm(`确定要重新处理文档 "${doc.filename}" 吗？`)) {
      try {
        const response = await api.post<{ message?: string }>(`/documents/${doc.id}/reprocess`)
        toast.success('重新处理任务已提交', response.message || '文档将在后台重新处理')
        queryClient.invalidateQueries({ queryKey: ['documents'] })
      } catch (error) {
        toast.error('重新处理失败', error instanceof Error ? error.message : '未知错误')
      }
    }
  }

  /** 批量删除选中的文档。 */
  async function batchDelete(): Promise<void> {
    const count = kbStore.selectedCount

    if (count === 0) {
      toast.warning('请先选择要删除的文档')
      return
    }

    if (confirm(`确定要删除选中的 ${count} 个文档吗？`)) {
      const ids = kbStore.selectedDocuments

      // 立即提示用户
      toast.info('正在删除文档...', `正在后台删除 ${count} 个文档`)

      // 清空选择（乐观更新）
      kbStore.clearSelection()

      // 调用API删除
      batchDeleteMutation.mutate(ids, {
        onSuccess: () => {
          // API已返回，但后端是异步删除
          console.log('[Batch Delete] Delete tasks submitted')
        },
        onError: (error) => {
          // 如果API失败，恢复文档列表
          toast.error('批量删除失败', error instanceof Error ? error.message : '未知错误')
          queryClient.invalidateQueries({ queryKey: ['documents'] })
          queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
        }
      })
    }
  }

  /** 切换文档上下架状态。 */
  function toggleStatus(doc: Document): void {
    const backendStatus = doc.status === 'active' ? 'draft' : 'published'
    const newStatusText = doc.status === 'active' ? '下架' : '上架'
    updateMutation.mutate({
      id: doc.id,
      data: { status: backendStatus }
    }, {
      onSuccess: () => {
        toast.success(`文档已成功${newStatusText}`)
      },
      onError: (error: unknown) => {
        toast.error(`${newStatusText}失败`, error instanceof Error ? error.message : '未知错误')
      }
    })
  }

  return {
    selectedCount,
    toggleSelectAll,
    deleteDocument,
    reprocessDocument,
    batchDelete,
    toggleStatus
  }
}
