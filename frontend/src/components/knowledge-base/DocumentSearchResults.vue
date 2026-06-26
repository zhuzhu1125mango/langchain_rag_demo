<template>
  <div class="border-t border-gray-200 dark:border-dark-600">
    <div class="p-4 bg-gray-50 dark:bg-dark-700 flex items-center justify-between">
      <div class="flex items-center gap-2">
        <Search class="w-4 h-4 text-gray-500" />
        <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
          搜索结果: "{{ query }}" ({{ results.length }} 条)
        </span>
      </div>
      <button
        @click="$emit('close')"
        class="flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
      >
        <ArrowLeft class="w-4 h-4" />
        返回列表
      </button>
    </div>

    <div v-if="isSearching" class="p-8 text-center">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500 mx-auto"></div>
    </div>

    <div v-else-if="results.length === 0" class="p-8 text-center">
      <BookOpen class="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
      <p class="text-gray-500 dark:text-gray-400">未找到匹配的内容</p>
    </div>

    <div v-else class="p-4 space-y-3 max-h-96 overflow-y-auto">
      <div
        v-for="result in results"
        :key="result.id + result.score"
        class="p-4 bg-white dark:bg-dark-800 rounded-lg border border-gray-200 dark:border-dark-600 hover:border-primary-300 dark:hover:border-primary-700 transition-colors"
      >
        <div class="flex items-start justify-between gap-4">
          <div class="flex-1">
            <div class="flex items-center gap-2 mb-2">
              <FileText class="w-4 h-4 text-gray-400" />
              <span class="font-medium text-gray-800 dark:text-white">{{ result.filename }}</span>
              <span v-if="result.kb_name" class="px-2 py-0.5 text-xs bg-gray-100 dark:bg-dark-600 text-gray-600 dark:text-gray-300 rounded">
                {{ result.kb_name }}
              </span>
              <span class="text-xs text-gray-400">相似度: {{ (result.score * 100).toFixed(1) }}%</span>
            </div>
            <p v-if="result.highlight" class="text-sm text-gray-600 dark:text-gray-300 mb-2">
              <span class="bg-yellow-100 dark:bg-yellow-900/30 px-1 rounded">{{ result.highlight }}</span>
            </p>
            <p class="text-sm text-gray-500 dark:text-gray-400 line-clamp-3">{{ result.content }}</p>
          </div>
          <button
            @click="$emit('preview', result.id)"
            class="p-2 text-gray-500 hover:text-primary-600 dark:hover:text-primary-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
            title="预览文档"
          >
            <Eye class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 文档搜索结果组件
 * @description 展示文档全文搜索结果列表（含命中高亮、相似度、所属知识库），并提供返回列表与预览入口。
 *
 * @props results - 搜索结果数组（含 filename/score/highlight/content 等）
 * @props query - 当前搜索关键词
 * @props isSearching - 是否正在搜索
 *
 * @emits close - 返回文档列表
 * @emits preview - 预览指定文档
 */
import { Search, ArrowLeft, BookOpen, FileText, Eye } from '@lucide/vue'
import type { SearchResult } from '@/queries/kb'

defineProps<{
  results: SearchResult[]
  query: string
  isSearching: boolean
}>()

defineEmits<{
  (e: 'close'): void
  (e: 'preview', docId: string): void
}>()
</script>
