import { ref } from 'vue'

/**
 * 通用"删除确认"弹窗状态管理。
 *
 * 适用于仅需确认删除、无新建/编辑表单的场景（如评价反馈删除）。
 * 仅管理 UI 状态；真正的删除 API 调用由调用方实现。
 *
 * @example
 * const { deleteConfirmVisible, deletingItem, confirmDelete, closeDeleteConfirm } =
 *   useDeleteConfirm<FeedbackItem>()
 */
export function useDeleteConfirm<T>() {
  const deleteConfirmVisible = ref(false)
  const deletingItem = ref<T | null>(null)

  function confirmDelete(item: T): void {
    deletingItem.value = item
    deleteConfirmVisible.value = true
  }

  function closeDeleteConfirm(): void {
    deleteConfirmVisible.value = false
    deletingItem.value = null
  }

  return {
    deleteConfirmVisible,
    deletingItem,
    confirmDelete,
    closeDeleteConfirm
  }
}

export interface UseCrudModalOptions<T, F> {
  /** 创建空白表单数据的工厂函数（用于新建场景）。 */
  createDefaultForm: () => F
  /** 将已有项转换为表单数据（用于编辑场景）。 */
  itemToForm: (item: T) => F
}

/**
 * 通用 CRUD 弹窗状态管理。
 *
 * 在 useDeleteConfirm 之上扩展新建/编辑表单弹窗，统一管理 modalVisible、
 * isEdit、editingId、formData 等状态。仅管理 UI 状态；具体的保存/删除
 * API 调用因涉及不同 mutation，由调用方实现。
 *
 * 表单数据使用深响应 ref，因此 `v-model="formData.xxx"` 形式的嵌套修改可被追踪。
 *
 * @example
 * const {
 *   modalVisible, isEdit, editingId, formData,
 *   openCreateModal, openEditModal, closeModal,
 *   deleteConfirmVisible, deletingItem, confirmDelete, closeDeleteConfirm
 * } = useCrudModal<Category, CategoryCreateRequest>({
 *   createDefaultForm: () => ({ name: '', ... }),
 *   itemToForm: (c) => ({ name: c.name, ... })
 * })
 */
export function useCrudModal<T extends { id: string }, F>(options: UseCrudModalOptions<T, F>) {
  const { createDefaultForm, itemToForm } = options

  const modalVisible = ref(false)
  const isEdit = ref(false)
  const editingId = ref<string | null>(null)
  const formData = ref<F>(createDefaultForm())

  const { deleteConfirmVisible, deletingItem, confirmDelete, closeDeleteConfirm } = useDeleteConfirm<T>()

  function openCreateModal(): void {
    isEdit.value = false
    editingId.value = null
    formData.value = createDefaultForm()
    modalVisible.value = true
  }

  function openEditModal(item: T): void {
    isEdit.value = true
    editingId.value = item.id
    formData.value = itemToForm(item)
    modalVisible.value = true
  }

  function closeModal(): void {
    modalVisible.value = false
  }

  return {
    modalVisible,
    isEdit,
    editingId,
    formData,
    openCreateModal,
    openEditModal,
    closeModal,
    deleteConfirmVisible,
    deletingItem,
    confirmDelete,
    closeDeleteConfirm
  }
}
