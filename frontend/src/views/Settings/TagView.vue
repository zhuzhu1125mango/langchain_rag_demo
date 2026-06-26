<template>
  <div class="flex-1 overflow-auto bg-gray-50 dark:bg-dark-900 p-6">
    <div class="max-w-4xl mx-auto">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-xl font-semibold text-gray-800 dark:text-white">标签管理</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">管理文档标签，支持自定义颜色</p>
        </div>
        <button
          @click="openCreateModal"
          class="flex items-center gap-2 px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
        >
          <Plus class="w-4 h-4" />
          新建标签
        </button>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="tag in tagsList"
          :key="tag.id"
          class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-4 hover:shadow-md transition-shadow"
        >
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-3">
              <div
                class="w-4 h-4 rounded-full flex-shrink-0"
                :style="{ backgroundColor: tag.color }"
              ></div>
              <span class="font-medium text-gray-800 dark:text-white">{{ tag.name }}</span>
            </div>
            <div class="flex items-center gap-1">
              <button
                @click="openEditModal(tag)"
                class="p-2 text-gray-400 hover:text-primary-500 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
                title="编辑"
              >
                <Edit class="w-4 h-4" />
              </button>
              <button
                @click="confirmDelete(tag)"
                class="p-2 text-gray-400 hover:text-red-500 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
                title="删除"
              >
                <Trash2 class="w-4 h-4" />
              </button>
            </div>
          </div>
          <div class="text-sm text-gray-500 dark:text-gray-400">
            十六进制: <code class="px-2 py-0.5 bg-gray-100 dark:bg-dark-700 rounded text-xs">{{ tag.color }}</code>
          </div>
        </div>
        <div
          v-if="tagsList.length === 0"
          class="col-span-full flex flex-col items-center justify-center py-16 text-gray-500 dark:text-gray-400"
        >
          <Tags class="w-12 h-12 mb-3 opacity-50" />
          <p>暂无标签</p>
          <p class="text-sm mt-1">点击上方按钮创建第一个标签</p>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="modalVisible"
      :title="isEdit ? '编辑标签' : '新建标签'"
      width="400px"
    >
      <form @submit.prevent="saveTag" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">名称 *</label>
          <input
            v-model="formData.name"
            type="text"
            required
            placeholder="请输入标签名称"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">颜色</label>
          <div class="flex items-center gap-3">
            <input
              v-model="formData.color"
              type="color"
              class="w-12 h-10 rounded cursor-pointer border border-gray-300"
            />
            <input
              v-model="formData.color"
              type="text"
              placeholder="#1890ff"
              class="flex-1 px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
            />
          </div>
          <div class="flex flex-wrap gap-2 mt-3">
            <button
              v-for="preset in presetColors"
              :key="preset"
              type="button"
              @click="formData.color = preset"
              class="w-6 h-6 rounded-full border-2 transition-transform hover:scale-110"
              :class="formData.color === preset ? 'border-gray-800 dark:border-white' : 'border-transparent'"
              :style="{ backgroundColor: preset }"
            ></button>
          </div>
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
          @click="saveTag"
          :disabled="!formData.name || createMutation.isPending.value || updateMutation.isPending.value"
          class="px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {{ isEdit ? '保存修改' : '创建标签' }}
        </button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="deleteConfirmVisible"
      title="确认删除"
      width="360px"
    >
      <div class="flex items-center gap-3 mb-2">
        <div
          class="w-4 h-4 rounded-full flex-shrink-0"
          :style="{ backgroundColor: deletingTag?.color }"
        ></div>
        <p class="text-gray-700 dark:text-gray-300">
          确定要删除标签 <span class="font-medium">{{ deletingTag?.name }}</span> 吗？
        </p>
      </div>
      <p class="text-sm text-gray-500 dark:text-gray-400">此操作无法撤销。</p>
      <template #footer>
        <button
          @click="deleteConfirmVisible = false"
          class="px-4 py-2 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
        >
          取消
        </button>
        <button
          @click="deleteTag"
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
 * 标签管理设置页。
 *
 * 提供标签的列表展示、新建/编辑（含预设颜色）、删除（二次确认）能力。
 */
import { ref, computed } from 'vue'
import { Plus, Edit, Trash2, Tags } from '@lucide/vue'
import { useTags, useCreateTag, useUpdateTag, useDeleteTag, type Tag, type TagCreateRequest } from '@/queries/kb'
import { useToast } from '@/composables/useToast'
import { useCrudModal } from '@/composables/useCrudModal'

const toast = useToast()

const { data: tags } = useTags()

const tagsList = computed(() => tags.value || [])

const createMutation = useCreateTag()
const updateMutation = useUpdateTag()
const deleteMutation = useDeleteTag()

const modalVisible = ref(false)
const deleteConfirmVisible = ref(false)
const isEdit = ref(false)
const editingId = ref<string | null>(null)
const deletingTag = ref<Tag | null>(null)

const formData = ref<TagCreateRequest>({
  name: '',
  color: '#1890ff'
})

const presetColors = [
  '#1890ff', '#52c41a', '#faad14', '#f5222d',
  '#722ed1', '#eb2f96', '#13c2c2', '#fa8c16',
  '#a0d911', '#eb3349', '#667eea', '#f093fb'
]

/** 打开新建标签弹窗，重置表单为默认值。 */
function openCreateModal(): void {
  isEdit.value = false
  editingId.value = null
  formData.value = {
    name: '',
    color: '#1890ff'
  }
  modalVisible.value = true
}

/** 打开编辑标签弹窗，回填当前标签数据。 */
function openEditModal(tag: Tag): void {
  isEdit.value = true
  editingId.value = tag.id
  formData.value = {
    name: tag.name,
    color: tag.color
  }
  modalVisible.value = true
}

/** 提交表单：编辑模式调用更新接口，否则调用创建接口，成功后关闭弹窗。 */
async function saveTag(): Promise<void> {
  if (!formData.value.name) return

  try {
    if (isEdit.value && editingId.value) {
      await updateMutation.mutateAsync({ id: editingId.value, data: formData.value })
      toast.success('标签更新成功')
    } else {
      await createMutation.mutateAsync(formData.value)
      toast.success('标签创建成功')
    }
    modalVisible.value = false
  } catch {
    toast.error('操作失败，请重试')
  }
}

/** 选中待删除标签并弹出二次确认框。 */
function confirmDelete(tag: Tag): void {
  deletingTag.value = tag
  deleteConfirmVisible.value = true
}

/** 确认删除当前选中标签，成功后关闭确认框并清空选中项。 */
async function deleteTag(): Promise<void> {
  if (!deletingTag.value) return

  try {
    await deleteMutation.mutateAsync(deletingTag.value.id)
    toast.success('标签删除成功')
    deleteConfirmVisible.value = false
    deletingTag.value = null
  } catch {
    toast.error('删除失败，请重试')
  }
}
</script>
