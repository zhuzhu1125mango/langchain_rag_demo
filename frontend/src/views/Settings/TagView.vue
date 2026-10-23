<template>
  <div class="flex-1 overflow-auto bg-page p-4 sm:p-6">
    <div class="max-w-4xl mx-auto">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-xl font-semibold text-content">标签管理</h1>
          <p class="text-sm text-content-muted mt-1">管理文档标签，支持自定义颜色</p>
        </div>
        <AppButton variant="primary" @click="openCreateModal">
          <Plus class="w-4 h-4" />
          新建标签
        </AppButton>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <AppCard
          v-for="tag in tagsList"
          :key="tag.id"
          hoverable
        >
          <div class="flex items-center justify-between mb-2">
            <div class="flex items-center gap-3">
              <div
                class="w-4 h-4 rounded-full flex-shrink-0"
                :style="{ backgroundColor: tag.color }"
              ></div>
              <span class="font-medium text-content">{{ tag.name }}</span>
            </div>
            <div class="flex items-center gap-1">
              <AppButton
                variant="ghost"
                size="icon"
                title="编辑"
                aria-label="编辑标签"
                @click="openEditModal(tag)"
              >
                <Edit class="w-4 h-4" />
              </AppButton>
              <AppButton
                variant="danger-ghost"
                size="icon"
                title="删除"
                aria-label="删除标签"
                @click="confirmDelete(tag)"
              >
                <Trash2 class="w-4 h-4" />
              </AppButton>
            </div>
          </div>
          <div class="text-sm text-content-muted">
            十六进制: <code class="px-2 py-0.5 bg-surface-subtle rounded text-xs">{{ tag.color }}</code>
          </div>
        </AppCard>
        <div
          v-if="tagsList.length === 0"
          class="col-span-full flex flex-col items-center justify-center py-16 text-content-muted"
        >
          <Tags class="w-12 h-12 mb-3 opacity-50" />
          <p>暂无标签</p>
          <p class="text-sm mt-1">点击上方按钮创建第一个标签</p>
        </div>
      </div>
    </div>

    <AppDialog
      v-model="modalVisible"
      :title="isEdit ? '编辑标签' : '新建标签'"
      width="400px"
    >
      <form @submit.prevent="saveTag" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">名称 *</label>
          <AppInput
            v-model="formData.name"
            variant="raised"
            required
            placeholder="请输入标签名称"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-2">颜色</label>
          <div class="flex items-center gap-3">
            <input
              v-model="formData.color"
              type="color"
              class="w-12 h-10 rounded cursor-pointer border border-line-strong"
            />
            <AppInput
              v-model="formData.color"
              variant="raised"
              placeholder="#1890ff"
              class="flex-1"
            />
          </div>
          <div class="flex flex-wrap gap-2 mt-3">
            <button
              v-for="preset in presetColors"
              :key="preset"
              type="button"
              @click="formData.color = preset"
              class="w-6 h-6 rounded-full border-2 transition-transform hover:scale-110"
              :class="formData.color === preset ? 'border-content' : 'border-transparent'"
              :style="{ backgroundColor: preset }"
            ></button>
          </div>
        </div>
      </form>
      <template #footer>
        <AppButton variant="ghost" @click="modalVisible = false">取消</AppButton>
        <AppButton
          variant="primary"
          :disabled="!formData.name || createMutation.isPending.value || updateMutation.isPending.value"
          :loading="createMutation.isPending.value || updateMutation.isPending.value"
          @click="saveTag"
        >
          {{ isEdit ? '保存修改' : '创建标签' }}
        </AppButton>
      </template>
    </AppDialog>

    <AppDialog
      v-model="deleteConfirmVisible"
      title="确认删除"
      width="360px"
    >
      <div class="flex items-center gap-3 mb-2">
        <div
          class="w-4 h-4 rounded-full flex-shrink-0"
          :style="{ backgroundColor: deletingTag?.color }"
        ></div>
        <p class="text-content-secondary">
          确定要删除标签 <span class="font-medium">{{ deletingTag?.name }}</span> 吗？
        </p>
      </div>
      <p class="text-sm text-content-muted">此操作无法撤销。</p>
      <template #footer>
        <AppButton variant="ghost" @click="deleteConfirmVisible = false">取消</AppButton>
        <AppButton
          variant="danger"
          :disabled="deleteMutation.isPending.value"
          :loading="deleteMutation.isPending.value"
          @click="deleteTag"
        >
          删除
        </AppButton>
      </template>
    </AppDialog>
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
import { AppButton, AppCard, AppDialog, AppInput } from '@/components/ui'

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
