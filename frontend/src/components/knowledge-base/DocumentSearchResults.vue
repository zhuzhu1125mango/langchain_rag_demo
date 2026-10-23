<template>
  <div class="border-t border-line">
    <div class="p-4 bg-surface-muted flex items-center justify-between">
      <div class="flex items-center gap-2">
        <Search class="w-4 h-4 text-gray-500" />
        <span class="text-sm font-medium text-content-secondary">
          搜索结果: "{{ query }}" ({{ results.length }} 条)
        </span>
      </div>
      <AppButton variant="ghost" size="sm" @click="$emit('close')">
        <ArrowLeft class="w-4 h-4" />
        返回列表
      </AppButton>
    </div>

    <AppStateView
      v-if="isSearching"
      state="loading"
      padding="md"
    />

    <AppStateView
      v-else-if="results.length === 0"
      state="empty"
      title="未找到匹配的内容"
      description="换个关键词再试试"
      :icon="BookOpen"
      padding="md"
    />

    <div v-else class="p-4 space-y-3 max-h-96 overflow-y-auto">
      <div
        v-for="result in results"
        :key="result.id + result.score"
        class="p-4 bg-surface rounded-lg border border-line hover:border-primary-300 dark:hover:border-primary-700 transition-colors"
      >
        <div class="flex items-start justify-between gap-4">
          <div class="flex-1">
            <div class="flex items-center gap-2 mb-2">
              <FileText class="w-4 h-4 text-gray-400" />
              <span class="font-medium text-content">{{ result.filename }}</span>
              <span v-if="result.kb_name" class="px-2 py-0.5 text-xs bg-surface-subtle-strong text-content-tertiary rounded">
                {{ result.kb_name }}
              </span>
              <span class="text-xs text-gray-400">相似度: {{ (result.score * 100).toFixed(1) }}%</span>
            </div>
            <p v-if="result.highlight" class="text-sm text-content-tertiary mb-2">
              <span class="bg-yellow-100 dark:bg-yellow-900/30 px-1 rounded">{{ result.highlight }}</span>
            </p>
            <p class="text-sm text-content-muted line-clamp-3">{{ result.content }}</p>
          </div>
          <AppButton
            variant="ghost"
            size="icon"
            title="预览文档"
            aria-label="预览文档"
            @click="$emit('preview', result.id)"
          >
            <Eye class="w-4 h-4" />
          </AppButton>
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
import { AppButton, AppStateView } from '@/components/ui'

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
