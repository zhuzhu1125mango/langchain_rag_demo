<template>
  <el-dialog
    :model-value="visible"
    @update:model-value="$emit('update:visible', $event)"
    :title="'文档预览: ' + displayFilename"
    width="900px"
  >
    <div class="space-y-3">
      <div class="flex items-center justify-between text-sm text-gray-500">
        <span>共 {{ totalPages }} 页</span>
        <div class="flex items-center gap-2">
          <button
            @click="loadPreviewPage(page - 1)"
            :disabled="page <= 1 || loading"
            class="px-2 py-1 text-xs bg-gray-100 dark:bg-dark-700 rounded hover:bg-gray-200 dark:hover:bg-dark-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            上一页
          </button>
          <span class="text-xs">第 {{ page }} / {{ totalPages }} 页</span>
          <button
            @click="loadPreviewPage(page + 1)"
            :disabled="page >= totalPages || loading"
            class="px-2 py-1 text-xs bg-gray-100 dark:bg-dark-700 rounded hover:bg-gray-200 dark:hover:bg-dark-600 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            下一页
          </button>
        </div>
      </div>
      <div class="max-h-96 overflow-auto bg-gray-50 dark:bg-dark-700 rounded-lg p-4">
        <div v-if="loading" class="flex items-center justify-center py-8">
          <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
        </div>
        <pre v-else class="whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-200 font-mono leading-relaxed">{{ content }}</pre>
      </div>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 文档预览对话框组件
 * @description 按需分页加载文档纯文本内容，支持上一页/下一页导航。
 * 文件名优先取外部传入（搜索结果预览），随后由接口返回值覆盖。
 *
 * @props visible - 对话框显隐（v-model:visible）
 * @props docId - 文档 ID
 * @props filename - 外部传入的文件名（可选，用于标题展示）
 *
 * @emits update:visible - 显隐变化
 */
import { ref, watch } from 'vue'
import { api } from '@/utils/axios'

const props = defineProps<{
  visible: boolean
  docId: string
  filename?: string
}>()

defineEmits<{
  (e: 'update:visible', value: boolean): void
}>()

const content = ref('')
const page = ref(1)
const totalPages = ref(1)
const loading = ref(false)
const displayFilename = ref('')

// 同步外部传入的文件名（搜索结果预览时初始为"加载中..."，随后由接口返回值覆盖）
watch(() => props.filename, (val) => {
  displayFilename.value = val || ''
}, { immediate: true })

// 弹窗打开且存在 docId 时加载第一页
watch([() => props.visible, () => props.docId], ([isVisible, docId]) => {
  if (isVisible && docId) {
    page.value = 1
    totalPages.value = 1
    loadPreviewPage(1)
  }
})

/** 加载文档预览的指定分页内容。 */
async function loadPreviewPage(targetPage: number): Promise<void> {
  if (!props.docId) return
  if (targetPage < 1 || (targetPage > totalPages.value && totalPages.value > 0) || loading.value) return

  page.value = targetPage
  loading.value = true

  try {
    // 按需分页预览，无需缓存，故直接调用 api 而未走 query hook
    const response = await api.get<{ content: string; total_pages: number; filename?: string }>(
      `/documents/${props.docId}/preview`,
      { params: { page: targetPage, page_size: 2000 } }
    )
    content.value = response.content || '暂无内容'
    totalPages.value = response.total_pages || 1
    if (response.filename) {
      displayFilename.value = response.filename
    }
  } catch (error) {
    content.value = `预览失败: ${error instanceof Error ? error.message : '未知错误'}`
    totalPages.value = 1
  } finally {
    loading.value = false
  }
}
</script>
