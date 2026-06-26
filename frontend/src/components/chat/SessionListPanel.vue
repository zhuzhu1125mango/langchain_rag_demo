<template>
  <aside
    :class="[
      'bg-white dark:bg-dark-800 border-r border-gray-200 dark:border-dark-600 transition-all duration-300 overflow-hidden flex-shrink-0',
      show ? 'w-72' : 'w-0'
    ]"
  >
    <div v-if="show" class="h-full flex flex-col">
      <div class="px-4 py-3 border-b border-gray-200 dark:border-dark-600 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <MessageSquare class="w-4 h-4 text-gray-500" />
          <span class="text-sm font-medium text-gray-700 dark:text-gray-300">会话列表</span>
          <span class="text-xs text-gray-400">({{ sessions?.length || 0 }})</span>
        </div>
        <div class="flex items-center gap-1">
          <button
            @click="toggleBatchMode"
            class="flex items-center gap-1 px-2 py-1 text-xs rounded-lg transition-colors"
            :class="isBatchMode
              ? 'bg-gray-100 dark:bg-dark-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-dark-600'
              : 'bg-gray-50 dark:bg-dark-700 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-600'"
          >
            {{ isBatchMode ? '完成' : '管理' }}
          </button>
          <button
            v-if="!isBatchMode"
            @click="emit('create-new-session')"
            class="flex items-center gap-1 px-2 py-1 text-xs bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400 rounded-lg hover:bg-primary-100 dark:hover:bg-primary-900/40 transition-colors"
          >
            <Plus class="w-3 h-3" />
            新建
          </button>
        </div>
      </div>
      <div class="flex-1 overflow-y-auto px-2 py-2">
        <div v-if="!sessions?.length" class="text-center py-8">
          <MessageSquare class="w-8 h-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
          <p class="text-xs text-gray-400">暂无会话</p>
          <button
            @click="emit('create-new-session')"
            class="mt-2 px-3 py-1 text-xs bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
          >
            开始新对话
          </button>
        </div>
        <div v-else class="space-y-1">
          <div
            v-for="session in sessions"
            :key="session.id"
            :class="[
              'group flex items-start gap-2 px-3 py-2 rounded-lg cursor-pointer transition-colors',
              currentSessionId === session.id && !isBatchMode
                ? 'bg-primary-50 dark:bg-primary-900/30 border border-primary-200 dark:border-primary-800'
                : 'hover:bg-gray-100 dark:hover:bg-dark-700 border border-transparent'
            ]"
            @click="isBatchMode ? toggleSelectSession(session) : emit('switch-session', session)"
          >
            <div
              v-if="isBatchMode"
              @click.stop="toggleSelectSession(session)"
              :class="[
                'w-4 h-4 rounded flex items-center justify-center flex-shrink-0 mt-0.5 transition-colors',
                selectedSessionIds.includes(session.id)
                  ? 'bg-primary-500 border border-primary-500'
                  : 'border border-gray-300 dark:border-dark-500 bg-white dark:bg-dark-700 hover:border-primary-400 dark:hover:border-primary-400'
              ]"
            >
              <Check v-if="selectedSessionIds.includes(session.id)" class="w-3 h-3 text-white" />
            </div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-1.5">
                <MessageSquare class="w-3 h-3 text-gray-400 flex-shrink-0 mt-0.5" />
                <span class="text-sm text-gray-700 dark:text-gray-200 font-medium truncate">{{ session.title || '未命名对话' }}</span>
              </div>
              <p class="text-xs text-gray-400 mt-0.5 truncate pl-5">{{ session.last_message || '暂无消息' }}</p>
              <p class="text-xs text-gray-400 mt-0.5 pl-5">{{ formatDate(session.updated_at) }} · {{ session.message_count || 0 }} 条消息</p>
            </div>
            <button
              v-if="!isBatchMode"
              @click.stop="emit('delete-session', session)"
              class="opacity-0 group-hover:opacity-100 p-1 text-gray-400 hover:text-red-500 transition-all flex-shrink-0 mt-0.5"
              title="删除会话"
            >
              <Trash2 class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>

      <!-- 批量删除操作栏 -->
      <div
        v-if="isBatchMode"
        class="px-3 py-2 border-t border-gray-200 dark:border-dark-600 bg-gray-50 dark:bg-dark-800 flex items-center justify-between"
      >
        <div class="flex items-center gap-2 cursor-pointer" @click="toggleSelectAll">
          <div
            :class="[
              'w-4 h-4 rounded flex items-center justify-center flex-shrink-0 transition-colors',
              isAllSelected
                ? 'bg-primary-500 border border-primary-500'
                : 'border border-gray-300 dark:border-dark-500 bg-white dark:bg-dark-700 hover:border-primary-400 dark:hover:border-primary-400'
            ]"
          >
            <Check v-if="isAllSelected" class="w-3 h-3 text-white" />
          </div>
          <span class="text-xs text-gray-600 dark:text-gray-300">全选</span>
          <span class="text-xs text-gray-400">已选 {{ selectedCount }} 条</span>
        </div>
        <button
          @click="onBatchDelete"
          :disabled="selectedCount === 0"
          :class="[
            'flex items-center gap-1 px-2 py-1 text-xs rounded-lg transition-colors',
            selectedCount > 0
              ? 'bg-red-50 dark:bg-red-900/30 text-red-600 dark:text-red-400 hover:bg-red-100 dark:hover:bg-red-900/40'
              : 'bg-gray-100 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
          ]"
        >
          <Trash2 class="w-3 h-3" />
          删除
        </button>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 会话列表组件
 * @description 在主对话区左侧展开，列出历史会话并支持切换、新建、单条删除；
 * 同时支持批量管理模式（多选 + 全选 + 批量删除）。
 *
 * @props show - 是否展开侧边栏
 * @props sessions - 会话列表
 * @props currentSessionId - 当前激活的会话 ID
 *
 * @emits create-new-session - 新建会话
 * @emits switch-session - 切换至指定会话
 * @emits delete-session - 删除单个会话
 * @emits batch-delete - 批量删除会话（携带 ID 列表）
 *
 * @expose resetBatchMode - 批量删除完成后由父组件调用，重置批量选择状态
 */
import { ref, computed } from 'vue'
import { MessageSquare, Plus, Check, Trash2 } from '@lucide/vue'
import type { Session } from '@/queries/chat'
import { formatDate } from '@/utils/format'

const props = defineProps<{
  show: boolean
  sessions?: Session[]
  currentSessionId?: string
}>()

const emit = defineEmits<{
  (e: 'create-new-session'): void
  (e: 'switch-session', session: Session): void
  (e: 'delete-session', session: Session): void
  (e: 'batch-delete', ids: string[]): void
}>()

// 批量管理模式及其选中状态为面板内部 UI 状态
const isBatchMode = ref(false)
const selectedSessionIds = ref<string[]>([])

const selectedCount = computed(() => selectedSessionIds.value.length)
const isAllSelected = computed(() => {
  if (!props.sessions || props.sessions.length === 0) return false
  return props.sessions.every(session => selectedSessionIds.value.includes(session.id))
})

/** 切换批量管理模式；退出时清空已选会话。 */
function toggleBatchMode(): void {
  isBatchMode.value = !isBatchMode.value
  if (!isBatchMode.value) {
    selectedSessionIds.value = []
  }
}

/** 切换单个会话的选中态（批量模式下使用）。 */
function toggleSelectSession(session: Session): void {
  const index = selectedSessionIds.value.indexOf(session.id)
  if (index === -1) {
    selectedSessionIds.value.push(session.id)
  } else {
    selectedSessionIds.value.splice(index, 1)
  }
}

/** 全选/取消全选：若已全选则清空，否则选中所有会话。 */
function toggleSelectAll(): void {
  if (isAllSelected.value) {
    selectedSessionIds.value = []
  } else {
    selectedSessionIds.value = props.sessions?.map(session => session.id) || []
  }
}

/** 触发批量删除事件，向父组件冒泡当前选中的会话 ID 列表副本。 */
function onBatchDelete(): void {
  if (selectedSessionIds.value.length === 0) return
  emit('batch-delete', [...selectedSessionIds.value])
}

/** 批量删除完成后由父组件调用，重置批量选择状态。 */
function resetBatchMode(): void {
  selectedSessionIds.value = []
  isBatchMode.value = false
}

defineExpose({ resetBatchMode })
</script>
