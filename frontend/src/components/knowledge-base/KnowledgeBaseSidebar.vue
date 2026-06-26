<template>
  <aside class="w-64 bg-white dark:bg-dark-800 border-r border-gray-200 dark:border-dark-600 flex flex-col">
    <div class="p-4 border-b border-gray-200 dark:border-dark-600">
      <h2 class="text-sm font-semibold text-gray-800 dark:text-white mb-3">知识库列表</h2>
      <button
        @click="$emit('create-kb')"
        class="w-full flex items-center justify-center gap-2 px-3 py-2 text-sm text-primary-600 dark:text-primary-400 bg-primary-50 dark:bg-primary-900/20 rounded-lg hover:bg-primary-100 dark:hover:bg-primary-900/30 transition-colors"
      >
        <Plus class="w-4 h-4" />
        <span>新建知识库</span>
      </button>
    </div>

    <div class="flex-1 overflow-y-auto p-2">
      <div
        v-for="kb in knowledgeBases"
        :key="kb.id"
        @click="$emit('select-kb', kb)"
        :class="[
          'p-3 rounded-lg cursor-pointer transition-colors mb-1',
          currentKB?.id === kb.id
            ? 'bg-primary-100 dark:bg-primary-900/30 border border-primary-300 dark:border-primary-700'
            : 'hover:bg-gray-100 dark:hover:bg-dark-700'
        ]"
      >
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <Database class="w-4 h-4 text-gray-400" />
            <span class="text-sm font-medium text-gray-800 dark:text-white">{{ kb.name }}</span>
          </div>
          <span v-if="kb.is_default" class="text-xs px-2 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400 rounded">
            默认
          </span>
        </div>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ kb.document_count }} 个文档</p>
      </div>

      <div v-if="!knowledgeBases?.length" class="p-4 text-center">
        <Database class="w-8 h-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
        <p class="text-sm text-gray-500 dark:text-gray-400">暂无知识库</p>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 知识库侧边栏组件
 * @description 知识库管理页左侧导航，展示所有知识库并支持新建、选中切换；当前选中项高亮显示。
 *
 * @props knowledgeBases - 知识库列表
 * @props currentKB - 当前选中的知识库（用于高亮）
 *
 * @emits select-kb - 选中某个知识库
 * @emits create-kb - 新建知识库
 */
import { Plus, Database } from '@lucide/vue'
import type { KnowledgeBase } from '@/queries/kb'

defineProps<{
  knowledgeBases: KnowledgeBase[]
  currentKB: KnowledgeBase | null
}>()

defineEmits<{
  (e: 'select-kb', kb: KnowledgeBase): void
  (e: 'create-kb'): void
}>()
</script>
