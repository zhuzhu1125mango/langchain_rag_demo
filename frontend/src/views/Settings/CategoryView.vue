<template>
  <div class="flex-1 overflow-auto bg-page p-4 sm:p-6">
    <div class="max-w-4xl mx-auto space-y-6">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-xl font-semibold text-content">分类管理</h1>
          <p class="text-sm text-content-muted mt-1">管理文档分类，支持多级分类结构</p>
        </div>
        <AppButton variant="primary" @click="openCreateModal">
          <Plus class="w-4 h-4" />
          新建分类
        </AppButton>
      </div>

      <AppCard padding="none">
        <div class="overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr class="border-b border-line">
                <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">名称</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">描述</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">父分类</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">排序</th>
                <th class="text-right py-3 px-4 text-sm font-medium text-content-muted">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="category in categoriesList"
                :key="category.id"
                class="border-b border-line-subtle hover:bg-surface-muted/50 transition-colors"
              >
                <td class="py-3 px-4">
                  <div class="flex items-center gap-2">
                    <Folder class="w-4 h-4 text-content-subtle" />
                    <span class="font-medium text-content">{{ category.name }}</span>
                  </div>
                </td>
                <td class="py-3 px-4 text-sm text-content-muted">
                  {{ category.description || '-' }}
                </td>
                <td class="py-3 px-4 text-sm text-content-muted">
                  {{ getParentName(category.parent_id) || '无' }}
                </td>
                <td class="py-3 px-4 text-sm text-content-muted">
                  {{ category.sort_order }}
                </td>
                <td class="py-3 px-4">
                  <div class="flex items-center justify-end gap-2">
                    <AppButton
                      variant="ghost"
                      size="icon"
                      title="编辑"
                      aria-label="编辑分类"
                      @click="openEditModal(category)"
                    >
                      <Edit class="w-4 h-4" />
                    </AppButton>
                    <AppButton
                      variant="danger-ghost"
                      size="icon"
                      title="删除"
                      aria-label="删除分类"
                      @click="confirmDelete(category)"
                    >
                      <Trash2 class="w-4 h-4" />
                    </AppButton>
                  </div>
                </td>
              </tr>
              <tr v-if="categoriesList.length === 0">
                <td colspan="5" class="py-8">
                  <AppStateView
                    state="empty"
                    title="暂无分类"
                    description="点击上方按钮创建第一个分类"
                    :icon="FolderX"
                    size="sm"
                    padding="sm"
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </AppCard>
    </div>

    <AppDialog
      v-model="modalVisible"
      :title="isEdit ? '编辑分类' : '新建分类'"
      width="480px"
    >
      <form @submit.prevent="saveCategory" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">名称 *</label>
          <AppInput
            v-model="formData.name"
            variant="raised"
            required
            placeholder="请输入分类名称"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">描述</label>
          <AppInput
            v-model="formData.description"
            variant="raised"
            multiline
            :rows="3"
            placeholder="请输入分类描述（可选）"
            class="resize-none"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">父分类</label>
          <AppSelect v-model="formData.parent_id" variant="raised" class="w-full">
            <option value="">无（顶级分类）</option>
            <option
              v-for="cat in parentOptions"
              :key="cat.id"
              :value="cat.id"
            >
              {{ cat.name }}
            </option>
          </AppSelect>
        </div>
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">排序</label>
          <AppInput
            v-model.number="formData.sort_order"
            type="number"
            variant="raised"
            placeholder="0"
          />
        </div>
      </form>
      <template #footer>
        <AppButton variant="ghost" @click="modalVisible = false">取消</AppButton>
        <AppButton
          variant="primary"
          :disabled="!formData.name || createMutation.isPending.value || updateMutation.isPending.value"
          :loading="createMutation.isPending.value || updateMutation.isPending.value"
          @click="saveCategory"
        >
          {{ isEdit ? '保存修改' : '创建分类' }}
        </AppButton>
      </template>
    </AppDialog>

    <AppDialog
      v-model="deleteConfirmVisible"
      title="确认删除"
      width="360px"
    >
      <p class="text-content-secondary">
        确定要删除分类 <span class="font-medium">{{ deletingCategory?.name }}</span> 吗？
      </p>
      <p class="text-sm text-content-muted mt-2">此操作无法撤销。</p>
      <template #footer>
        <AppButton variant="ghost" @click="deleteConfirmVisible = false">取消</AppButton>
        <AppButton
          variant="danger"
          :disabled="deleteMutation.isPending.value"
          :loading="deleteMutation.isPending.value"
          @click="deleteCategory"
        >
          删除
        </AppButton>
      </template>
    </AppDialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 分类管理设置页。
 *
 * 以表格展示分类列表，支持多级分类结构（通过父分类关联），
 * 提供新建/编辑/删除能力，删除前进行二次确认。
 */
import { computed } from 'vue'
import { Plus, Edit, Trash2, Folder, FolderX } from '@lucide/vue'
import { useCategories, useCreateCategory, useUpdateCategory, useDeleteCategory, type Category, type CategoryCreateRequest } from '@/queries/kb'
import { useCrudModal } from '@/composables/useCrudModal'
import { useToast } from '@/composables/useToast'
import { AppButton, AppCard, AppDialog, AppInput, AppSelect, AppStateView } from '@/components/ui'

const toast = useToast()

const { data: categories } = useCategories()

const categoriesList = computed(() => categories.value || [])

const createMutation = useCreateCategory()
const updateMutation = useUpdateCategory()
const deleteMutation = useDeleteCategory()

const {
  modalVisible,
  deleteConfirmVisible,
  isEdit,
  editingId,
  deletingItem: deletingCategory,
  formData,
  openCreateModal,
  openEditModal,
  closeModal,
  confirmDelete,
  closeDeleteConfirm
} = useCrudModal<Category, CategoryCreateRequest>({
  createDefaultForm: () => ({
    name: '',
    description: '',
    parent_id: '',
    sort_order: 0
  }),
  itemToForm: (category) => ({
    name: category.name,
    description: category.description || '',
    parent_id: category.parent_id || '',
    sort_order: category.sort_order
  })
})

const parentOptions = computed(() => {
  return categoriesList.value.filter(c => c.id !== editingId.value)
})

/** 根据父分类 ID 查找父分类名称，无父分类时返回 undefined。 */
function getParentName(parentId?: string): string | undefined {
  if (!parentId) return undefined
  return categoriesList.value.find(c => c.id === parentId)?.name
}

/** 提交表单：编辑模式调用更新接口，否则调用创建接口，成功后关闭弹窗。 */
async function saveCategory(): Promise<void> {
  if (!formData.value.name) return

  try {
    if (isEdit.value && editingId.value) {
      await updateMutation.mutateAsync({ id: editingId.value, data: formData.value })
      toast.success('分类更新成功')
    } else {
      await createMutation.mutateAsync(formData.value)
      toast.success('分类创建成功')
    }
    closeModal()
  } catch {
    toast.error('操作失败，请重试')
  }
}

/** 确认删除当前选中分类，成功后关闭确认框。 */
async function deleteCategory(): Promise<void> {
  if (!deletingCategory.value) return

  try {
    await deleteMutation.mutateAsync(deletingCategory.value.id)
    toast.success('分类删除成功')
    closeDeleteConfirm()
  } catch {
    toast.error('删除失败，请重试')
  }
}
</script>
