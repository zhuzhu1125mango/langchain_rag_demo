<template>
  <div class="bg-surface border-b border-line px-4 sm:px-6 py-4">
    <div class="mb-3">
      <div class="relative">
        <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
        <input
          v-model="kbSearchQuery"
          type="text"
          placeholder="搜索知识库..."
          class="w-full pl-10 pr-4 py-2 bg-surface-subtle text-content placeholder-gray-400 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
        />
        <button
          v-if="kbSearchQuery"
          @click="kbSearchQuery = ''"
          class="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-content-tertiary"
        >
          <X class="w-4 h-4" />
        </button>
      </div>
    </div>

    <div v-if="groupedKBs.length > 0" class="space-y-3">
      <div v-for="group in groupedKBs" :key="group.id || 'ungrouped'" class="space-y-1">
        <div v-if="group.name" class="flex items-center gap-2 px-2 py-1">
          <Folder class="w-4 h-4 text-gray-400" />
          <span class="text-xs font-medium text-content-muted">{{ group.name }}</span>
          <span class="text-xs text-gray-400">({{ group.kbs?.length || 0 }})</span>
        </div>
        <div class="flex flex-wrap gap-2 pl-2" :class="group.name ? 'pl-6' : ''">
          <button
            v-for="kb in group.kbs"
            :key="kb.id"
            @click="toggleKB(kb)"
            :class="[
              'px-3 py-1.5 rounded-full text-sm transition-all flex items-center gap-1.5',
              selectedKBs.includes(kb.id)
                ? 'bg-primary-500 text-white'
                : 'bg-surface-subtle text-content-tertiary hover:bg-surface-hover-strong'
            ]"
          >
            <Database class="w-3.5 h-3.5" />
            <span>{{ kb.name }}</span>
            <span v-if="kb.document_count" class="text-xs opacity-70">({{ kb.document_count }})</span>
          </button>
        </div>
      </div>
    </div>
    <div v-else-if="filteredKBs.length > 0" class="flex flex-wrap gap-2">
      <button
        v-for="kb in filteredKBs"
        :key="kb.id"
        @click="toggleKB(kb)"
        :class="[
          'px-3 py-1.5 rounded-full text-sm transition-all flex items-center gap-1.5',
          selectedKBs.includes(kb.id)
            ? 'bg-primary-500 text-white'
            : 'bg-surface-subtle text-content-tertiary hover:bg-surface-hover-strong'
        ]"
      >
        <Database class="w-3.5 h-3.5" />
        <span>{{ kb.name }}</span>
        <span v-if="kb.document_count" class="text-xs opacity-70">({{ kb.document_count }})</span>
      </button>
    </div>
    <div v-else class="text-center py-4">
      <Database class="w-8 h-8 text-content-disabled mx-auto mb-2" />
      <p class="text-sm text-gray-400">未找到匹配的知识库</p>
    </div>

    <button
      v-if="selectedKBs.length > 0"
      @click="clearSelection"
      class="mt-3 px-4 py-2 text-sm bg-surface-subtle text-content-tertiary rounded-lg hover:bg-surface-hover-strong transition-colors"
    >
      清除选择 ({{ selectedKBs.length }})
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * 知识库多选组件
 * @description 提供搜索过滤、分组聚合展示与多选切换；通过 v-model:modelValue 与父组件同步选中态。
 *
 * @props knowledgeBases - 可选知识库全集列表
 * @props modelValue - 当前已选中的知识库 ID 列表（v-model）
 *
 * @emits update:modelValue - 选中列表变化时触发
 */
import { ref, computed } from 'vue'
import { Search, X, Folder, Database } from '@lucide/vue'
import type { KnowledgeBase } from '@/queries/kb'

const props = defineProps<{
  knowledgeBases?: KnowledgeBase[]
  modelValue: string[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string[]): void
}>()

// 搜索词为选择器内部 UI 状态
const kbSearchQuery = ref('')

// 选中知识库双向同步到父组件
const selectedKBs = computed<string[]>({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/** 计算属性：按搜索关键词过滤知识库（匹配 name 或 description，大小写不敏感）。无关键词时返回全集。 */
const filteredKBs = computed(() => {
  if (!props.knowledgeBases || kbSearchQuery.value.trim() === '') {
    return props.knowledgeBases || []
  }
  const query = kbSearchQuery.value.toLowerCase()
  return props.knowledgeBases.filter(kb =>
    kb.name.toLowerCase().includes(query) ||
    kb.description?.toLowerCase().includes(query)
  )
})

// 将知识库按分组聚合，未分组显示为平铺列表
// D12 修复：分组 key 用 group_id（稳定唯一），显示名用 group_name——此前直接用
// group_id 当显示名，有 group_id 的分组会渲染出 UUID。key 与显示名解耦。
const groupedKBs = computed(() => {
  if (!filteredKBs.value || filteredKBs.value.length === 0) {
    return []
  }

  const groups: Record<string, { name: string; items: KnowledgeBase[] }> = {}

  filteredKBs.value.forEach(kb => {
    const groupKey = kb.group_id || 'ungrouped'
    if (!groups[groupKey]) {
      groups[groupKey] = {
        name: kb.group_name || (kb.group_id ? kb.group_id : ''),
        items: []
      }
    }
    groups[groupKey].items.push(kb)
  })

  return Object.entries(groups).map(([id, group]) => ({
    id,
    name: id === 'ungrouped' ? '' : group.name,
    kbs: group.items
  }))
})

/** 切换某个知识库的选中态：未选则加入，已选则移除，并通过 setter 同步到父组件。 */
function toggleKB(kb: KnowledgeBase): void {
  const arr = [...selectedKBs.value]
  const index = arr.indexOf(kb.id)
  if (index === -1) {
    arr.push(kb.id)
  } else {
    arr.splice(index, 1)
  }
  selectedKBs.value = arr
}

/** 清空全部选中知识库。 */
function clearSelection(): void {
  selectedKBs.value = []
}
</script>
