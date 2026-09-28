import { ref, watch } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useKBStore } from '@/stores/kb'
import {
  useCreateKnowledgeBase,
  useUpdateKnowledgeBase,
  useDeleteKnowledgeBase,
  useBatchDeleteKnowledgeBases
} from '@/queries/kb'
import { useToast, handleMutationError } from '@/composables/useToast'
import { ElMessageBox } from 'element-plus'
import type { Ref } from 'vue'

/**
 * 知识库管理弹窗编排（从 KnowledgeBaseView.vue 抽取，行为不变）。
 *
 * 覆盖：创建/编辑知识库弹窗状态与 CRUD、删除当前知识库、批量删除知识库；
 * 编辑表单随当前选中知识库自动回填。
 */
export function useKbManage(currentKB: Ref<{ id: string; name: string; description?: string } | null>) {
  const kbStore = useKBStore()
  const queryClient = useQueryClient()
  const toast = useToast()

  const showCreateKBModal = ref(false)
  const showEditKBModal = ref(false)
  const newKBName = ref('')
  const newKBDescription = ref('')
  const editKBName = ref('')
  const editKBDescription = ref('')

  const createKBMutation = useCreateKnowledgeBase()
  const updateKBMutation = useUpdateKnowledgeBase()
  const deleteKBMutation = useDeleteKnowledgeBase()
  const batchDeleteKBMutation = useBatchDeleteKnowledgeBases()

  /** 创建新知识库并切换到该知识库。 */
  function createKnowledgeBase(): void {
    if (!newKBName.value.trim()) {
      toast.warning('请输入知识库名称')
      return
    }

    createKBMutation.mutate({
      name: newKBName.value.trim(),
      description: newKBDescription.value.trim() || undefined
    }, {
      onSuccess: (newKB) => {
        toast.success('知识库创建成功')
        newKBName.value = ''
        newKBDescription.value = ''
        showCreateKBModal.value = false
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
        if (newKB) {
          kbStore.setCurrentKB(newKB)
        }
      },
      onError: (error) => {
        handleMutationError(error, '创建失败')
      }
    })
  }

  watch(currentKB, (kb) => {
    if (kb) {
      editKBName.value = kb.name
      editKBDescription.value = kb.description || ''
    }
  })

  /** 更新当前知识库名称与描述。 */
  function updateKnowledgeBase(): void {
    if (!editKBName.value.trim()) {
      toast.warning('请输入知识库名称')
      return
    }

    if (!kbStore.currentKB) return

    updateKBMutation.mutate({
      id: kbStore.currentKB.id,
      data: {
        name: editKBName.value.trim(),
        description: editKBDescription.value.trim() || undefined
      }
    }, {
      onSuccess: () => {
        toast.success('知识库更新成功')
        showEditKBModal.value = false
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      },
      onError: (error) => {
        handleMutationError(error, '更新失败')
      }
    })
  }

  /** 删除当前选中的知识库及其全部文档。 */
  function deleteKnowledgeBaseConfirm(): void {
    if (!kbStore.currentKB) return

    if (confirm(`确定要删除知识库 "${kbStore.currentKB.name}" 吗？这将删除该知识库中的所有文档。`)) {
      deleteKBMutation.mutate(kbStore.currentKB.id, {
        onSuccess: () => {
          toast.success('知识库删除成功')
          kbStore.setCurrentKB(null)
          queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
          queryClient.invalidateQueries({ queryKey: ['documents'] })
        },
        onError: (error) => {
          handleMutationError(error, '删除失败')
        }
      })
    }
  }

  /** 批量删除选中的知识库。 */
  async function batchDeleteKnowledgeBases(): Promise<void> {
    const selectedCount = kbStore.selectedKBCount

    if (selectedCount === 0) {
      toast.warning('请先选择要删除的知识库')
      return
    }

    try {
      await ElMessageBox.confirm(
        `确定要删除选中的 ${selectedCount} 个知识库吗？其中的所有文档也将被删除，操作不可恢复。`,
        '批量删除确认',
        {
          confirmButtonText: '删除',
          cancelButtonText: '取消',
          type: 'warning',
          confirmButtonClass: 'el-button--danger'
        }
      )
    } catch {
      return
    }

    const ids = kbStore.selectedKBs
    toast.info('正在删除知识库...', `正在后台删除 ${selectedCount} 个知识库`)

    batchDeleteKBMutation.mutate(ids, {
      onSuccess: (result) => {
        toast.success(
          '批量删除成功',
          `已删除 ${result.deleted_count} 个知识库${result.skipped_ids.length ? `，跳过 ${result.skipped_ids.length} 个` : ''}`
        )
        kbStore.exitBatchKBMode()
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
        queryClient.invalidateQueries({ queryKey: ['documents'] })
      },
      onError: (error) => {
        handleMutationError(error, '批量删除失败')
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      }
    })
  }

  return {
    showCreateKBModal,
    showEditKBModal,
    newKBName,
    newKBDescription,
    editKBName,
    editKBDescription,
    createKnowledgeBase,
    updateKnowledgeBase,
    deleteKnowledgeBaseConfirm,
    batchDeleteKnowledgeBases
  }
}
