<template>
  <div class="flex-1 overflow-auto bg-gray-50 dark:bg-dark-900 p-6">
    <div class="max-w-4xl mx-auto">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-xl font-semibold text-gray-800 dark:text-white">分类管理</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">管理文档分类，支持多级分类结构</p>
        </div>
        <button
          @click="openCreateModal"
          class="flex items-center gap-2 px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
        >
          <Plus class="w-4 h-4" />
          新建分类
        </button>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600">
        <div class="overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr class="border-b border-gray-200 dark:border-dark-600">
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">名称</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">描述</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">父分类</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">排序</th>
                <th class="text-right py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="category in categoriesList"
                :key="category.id"
                class="border-b border-gray-100 dark:border-dark-700 hover:bg-gray-50 dark:hover:bg-dark-700/50 transition-colors"
              >
                <td class="py-3 px-4">
                  <div class="flex items-center gap-2">
                    <Folder class="w-4 h-4 text-gray-400" />
                    <span class="font-medium text-gray-800 dark:text-white">{{ category.name }}</span>
                  </div>
                </td>
                <td class="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                  {{ category.description || '-' }}
                </td>
                <td class="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                  {{ getParentName(category.parent_id) || '无' }}
                </td>
                <td class="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                  {{ category.sort_order }}
                </td>
                <td class="py-3 px-4">
                  <div class="flex items-center justify-end gap-2">
                    <button
                      @click="openEditModal(category)"
                      class="p-2 text-gray-500 hover:text-primary-500 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
                      title="编辑"
                    >
                      <Edit class="w-4 h-4" />
                    </button>
                    <button
                      @click="confirmDelete(category)"
                      class="p-2 text-gray-500 hover:text-red-500 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
                      title="删除"
                    >
                      <Trash2 class="w-4 h-4" />
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="categoriesList.length === 0">
                <td colspan="5" class="py-12 text-center text-gray-500 dark:text-gray-400">
                  <FolderX class="w-12 h-12 mx-auto mb-3 opacity-50" />
                  <p>暂无分类</p>
                  <p class="text-sm mt-1">点击上方按钮创建第一个分类</p>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="modalVisible"
      :title="isEdit ? '编辑分类' : '新建分类'"
      width="480px"
    >
      <form @submit.prevent="saveCategory" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">名称 *</label>
          <input
            v-model="formData.name"
            type="text"
            required
            placeholder="请输入分类名称"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">描述</label>
          <textarea
            v-model="formData.description"
            rows="3"
            placeholder="请输入分类描述（可选）"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
          ></textarea>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">父分类</label>
          <select
            v-model="formData.parent_id"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
          >
            <option value="">无（顶级分类）</option>
            <option
              v-for="cat in parentOptions"
              :key="cat.id"
              :value="cat.id"
            >
              {{ cat.name }}
            </option>
          </select>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">排序</label>
          <input
            v-model.number="formData.sort_order"
            type="number"
            min="0"
            placeholder="0"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
      </form>
      <template #footer>
        <button
          @click="modalVisible = false"
          class="px-4 py-2 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
        >
          取消
        </button>
        <button
          @click="saveCategory"
          :disabled="!formData.name || createMutation.isPending.value || updateMutation.isPending.value"
          class="px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ isEdit ? '保存修改' : '创建分类' }}
        </button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="deleteConfirmVisible"
      title="确认删除"
      width="360px"
    >
      <p class="text-gray-700 dark:text-gray-300">
        确定要删除分类 <span class="font-medium">{{ deletingCategory?.name }}</span> 吗？
      </p>
      <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">此操作无法撤销。</p>
      <template #footer>
        <button
          @click="deleteConfirmVisible = false"
          class="px-4 py-2 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
        >
          取消
        </button>
        <button
          @click="deleteCategory"
          :disabled="deleteMutation.isPending.value"
          class="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          删除
        </button>
      </template>
    </el-dialog>
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
