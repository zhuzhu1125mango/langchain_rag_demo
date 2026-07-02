<template>
  <aside class="w-64 bg-white dark:bg-dark-800 border-r border-gray-200 dark:border-dark-600 flex flex-col">
    <div class="p-4 border-b border-gray-200 dark:border-dark-600">
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-sm font-semibold text-gray-800 dark:text-white">知识库列表</h2>
        <button
          v-if="!isBatchKBMode"
          @click="toggleBatchMode"
          class="text-xs text-gray-500 dark:text-gray-400 hover:text-primary-600 dark:hover:text-primary-400"
        >
          管理
        </button>
        <button
          v-else
          @click="toggleBatchMode"
          class="text-xs text-primary-600 dark:text-primary-400 hover:text-primary-700 dark:hover:text-primary-300"
        >
          完成
        </button>
      </div>
      <button
        v-if="!isBatchKBMode"
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
        @click="handleClick(kb)"
        :class="[
          'p-3 rounded-lg cursor-pointer transition-colors mb-1',
          currentKB?.id === kb.id && !isBatchKBMode
            ? 'bg-primary-100 dark:bg-primary-900/30 border border-primary-300 dark:border-primary-700'
            : 'hover:bg-gray-100 dark:hover:bg-dark-700'
        ]"
      >
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <input
              v-if="isBatchKBMode"
              type="checkbox"
              :checked="kbStore.isKBSelected(kb)"
              @click.stop
              @change="kbStore.selectKB(kb)"
              class="rounded border-gray-300 dark:border-dark-500 bg-gray-100 dark:bg-dark-700 text-primary-600 focus:ring-primary-500"
            />
            <Database class="w-4 h-4 text-gray-400" />
            <span class="text-sm font-medium text-gray-800 dark:text-white">{{ kb.name }}</span>
          </div>
          <span v-if="kb.is_default && !isBatchKBMode" class="text-xs px-2 py-0.5 bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400 rounded">
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

    <!-- 批量操作栏 -->
    <div
      v-if="isBatchKBMode"
      class="p-3 border-t border-gray-200 dark:border-dark-600 bg-gray-50 dark:bg-dark-900"
    >
      <div class="flex items-center justify-between mb-2">
        <label class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300 cursor-pointer">
          <input
            type="checkbox"
            :checked="isAllSelected"
            @change="toggleSelectAll"
            class="rounded border-gray-300 dark:border-dark-500 bg-gray-100 dark:bg-dark-700 text-primary-600 focus:ring-primary-500"
          />
          <span>全选</span>
        </label>
        <span class="text-xs text-gray-400">已选 {{ kbStore.selectedKBCount }} 个</span>
      </div>
      <button
        @click="$emit('batch-delete')"
        :disabled="kbStore.selectedKBCount === 0"
        :class="[
          'w-full flex items-center justify-center gap-2 px-3 py-2 text-sm rounded-lg transition-colors',
          kbStore.selectedKBCount > 0
            ? 'bg-red-500 text-white hover:bg-red-600'
            : 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
        ]"
      >
        <Trash2 class="w-4 h-4" />
        <span>批量删除 ({{ kbStore.selectedKBCount }})</span>
      </button>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 知识库侧边栏组件
 * @description 知识库管理页左侧导航，展示所有知识库并支持新建、选中切换；
 * 新增批量管理模式，支持多选 knowledge_base 后触发批量删除。
 *
 * @props knowledgeBases - 知识库列表
 * @props currentKB - 当前选中的知识库（用于高亮）
 *
 * @emits select-kb - 选中某个知识库
 * @emits create-kb - 新建知识库
 * @emits batch-delete - 触发批量删除知识库
 */
import { computed } from 'vue'
import { Plus, Database, Trash2 } from '@lucide/vue'
import { useKBStore } from '@/stores/kb'
import type { KnowledgeBase } from '@/queries/kb'

const props = defineProps<{
  knowledgeBases: KnowledgeBase[]
  currentKB: KnowledgeBase | null
}>()

const emit = defineEmits<{
  (e: 'select-kb', kb: KnowledgeBase): void
  (e: 'create-kb'): void
  (e: 'batch-delete'): void
}>()

const kbStore = useKBStore()
const isBatchKBMode = computed(() => kbStore.isBatchKBMode)

const isAllSelected = computed(() => {
  return props.knowledgeBases.length > 0 && props.knowledgeBases.every(kb => kbStore.isKBSelected(kb))
})

function toggleBatchMode() {
  kbStore.toggleBatchKBMode()
}

function handleClick(kb: KnowledgeBase) {
  if (isBatchKBMode.value) {
    kbStore.selectKB(kb)
  } else {
    emit('select-kb', kb)
  }
}

function toggleSelectAll(event: Event) {
  const target = event.target as HTMLInputElement
  if (target.checked) {
    kbStore.selectAllKBs(props.knowledgeBases)
  } else {
    kbStore.clearKBSelection()
  }
}
</script>
