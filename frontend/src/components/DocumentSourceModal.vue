<template>
  <Teleport to="body">
    <div 
      v-if="visible" 
      class="fixed inset-0 z-50 flex items-center justify-center p-4"
      @click.self="close"
    >
      <div class="absolute inset-0 bg-black/50 backdrop-blur-sm"></div>
      <div class="relative bg-white dark:bg-dark-800 rounded-2xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col animate-scale-in">
        <div class="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-dark-600">
          <div class="flex items-center gap-3">
            <FileText class="w-5 h-5 text-primary-500" />
            <div>
              <h3 class="text-lg font-semibold text-gray-800 dark:text-white">{{ sourceData?.filename }}</h3>
              <p class="text-sm text-gray-500 dark:text-gray-400">
                第 {{ (sourceData?.chunk_index ?? 0) + 1 }} / {{ sourceData?.total_chunks ?? 0 }} 段
              </p>
            </div>
          </div>
          <button
            @click="close"
            class="p-2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 rounded-lg hover:bg-gray-100 dark:hover:bg-dark-700 transition-colors"
          >
            <X class="w-5 h-5" />
          </button>
        </div>
        
        <div class="flex-1 overflow-y-auto p-6">
          <div v-if="isLoading" class="flex flex-col items-center justify-center py-12">
            <Loader2 class="w-8 h-8 text-primary-500 animate-spin" />
            <p class="mt-3 text-sm text-gray-500 dark:text-gray-400">加载中...</p>
          </div>
          <div v-else-if="sourceData" class="space-y-4">
            <div v-if="sourceData.kb_name" class="inline-flex items-center gap-2 px-3 py-1.5 bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 rounded-full text-sm">
              <Database class="w-4 h-4" />
              <span>{{ sourceData.kb_name }}</span>
            </div>
            
            <div class="relative">
              <div class="absolute -left-3 top-0 bottom-0 w-0.5 bg-primary-500 rounded-full"></div>
              <div class="pl-4">
                <h4 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">上下文内容</h4>
                <div class="bg-gray-50 dark:bg-dark-700 rounded-xl p-4">
                  <p 
                    class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed"
                    v-html="highlightedContent"
                  ></p>
                </div>
              </div>
            </div>
            
            <div class="relative">
              <div class="absolute -left-3 top-0 bottom-0 w-0.5 bg-green-500 rounded-full"></div>
              <div class="pl-4">
                <h4 class="text-sm font-medium text-gray-500 dark:text-gray-400 mb-2">引用段落</h4>
                <div class="bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-800 rounded-xl p-4">
                  <p class="text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">
                    {{ sourceData.content }}
                  </p>
                </div>
              </div>
            </div>
          </div>
          <div v-else class="flex flex-col items-center justify-center py-12">
            <FileQuestion class="w-12 h-12 text-gray-300 dark:text-gray-600 mb-3" />
            <p class="text-sm text-gray-500 dark:text-gray-400">无法获取文档内容</p>
          </div>
        </div>
        
        <div class="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200 dark:border-dark-600">
          <button
            @click="close"
            class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            关闭
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * 文档来源详情弹窗组件
 * @description 在对话中点击参考来源时弹出，展示该来源所属文档的上下文内容，
 * 并基于 highlight_offset/highlight_length 在 surrounding_content 中高亮命中段落。
 * 上下文与引用段落分别用蓝/绿色块区分展示。
 *
 * @props visible - 弹窗显隐
 * @props docId - 文档 ID
 * @props chunkIndex - 命中的分块索引（从 0 开始）
 *
 * @emits close - 关闭弹窗
 */
import { ref, computed, watch } from 'vue'
import { FileText, X, Database, Loader2, FileQuestion } from '@lucide/vue'
import { useGetDocumentSource } from '@/queries/kb'

/** 文档来源数据结构 */
interface SourceData {
  /** 文档 ID */
  doc_id: string
  /** 文件名 */
  filename: string
  /** 所属知识库 ID（可选） */
  kb_id?: string
  /** 所属知识库名称（可选） */
  kb_name?: string
  /** 当前命中的分块索引（从 0 开始） */
  chunk_index: number
  /** 文档总分块数 */
  total_chunks: number
  /** 命中分块的原文内容（绿色引用段落） */
  content: string
  /** 命中分块周围的上下文内容（用于在蓝色上下文块中高亮展示） */
  surrounding_content?: string
  /** 高亮起始偏移量（相对 surrounding_content，0 基），用于定位命中片段 */
  highlight_offset?: number
  /** 高亮片段长度（从 highlight_offset 起计算），未提供时按 0 处理 */
  highlight_length?: number
}

const props = defineProps<{
  visible: boolean
  docId: string
  chunkIndex: number
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const isLoading = ref(false)
const sourceData = ref<SourceData | null>(null)

const getSource = useGetDocumentSource()

/** HTML 转义：将 & < > " ' 替换为对应实体，防止 surrounding_content 中的用户内容引发 XSS。 */
function escapeHtml(str: string): string {
  return str.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]!))
}

const highlightedContent = computed(() => {
  if (!sourceData.value?.surrounding_content || !sourceData.value.highlight_offset) {
    return ''
  }

  const content = sourceData.value.surrounding_content
  const offset = sourceData.value.highlight_offset
  const length = sourceData.value.highlight_length || 0

  return escapeHtml(content.substring(0, offset)) +
         '<mark class="bg-yellow-200 dark:bg-yellow-800/50 text-gray-900 dark:text-yellow-100 px-0.5 rounded">' +
         escapeHtml(content.substring(offset, offset + length)) +
         '</mark>' +
         escapeHtml(content.substring(offset + length))
})

/** 加载指定文档与分块的来源数据；无效参数时直接返回。 */
async function loadSource() {
  if (!props.docId || props.chunkIndex < 0) return

  isLoading.value = true

  try {
    const result = await getSource(props.docId, props.chunkIndex)
    sourceData.value = result
  } catch (err) {
    console.error('获取文档来源失败:', err)
  } finally {
    isLoading.value = false
  }
}

/** 关闭弹窗：仅向父组件冒泡 close 事件。 */
function close() {
  emit('close')
}

watch(() => [props.visible, props.docId, props.chunkIndex], ([visible]) => {
  if (visible) {
    loadSource()
  }
})
</script>

<style scoped>
.animate-scale-in {
  animation: scaleIn 0.2s ease-out;
}

@keyframes scaleIn {
  from {
    opacity: 0;
    transform: scale(0.95);
  }
  to {
    opacity: 1;
    transform: scale(1);
  }
}
</style>
