<template>
  <div class="flex-1 flex flex-col h-full bg-gray-50 dark:bg-dark-900 overflow-hidden">
    <header class="bg-white dark:bg-dark-800 border-b border-gray-200 dark:border-dark-600 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h1 class="text-xl font-semibold text-gray-800 dark:text-white">历史对话</h1>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">查看和管理您的历史对话记录</p>
        </div>
        <button
          @click="clearAllHistory"
          :disabled="!sessionsData?.length"
          :class="[
            'flex items-center gap-2 px-4 py-2 text-sm rounded-lg transition-colors',
            sessionsData?.length
              ? 'bg-red-500 text-white hover:bg-red-600'
              : 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
          ]"
        >
          <Trash2 class="w-4 h-4" />
          <span>清空全部</span>
        </button>
      </div>
    </header>

    <div class="flex-1 overflow-auto p-6">
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <div
          v-for="session in sessionsData"
          :key="session.id"
          @click="openSession(session)"
          :class="[
            'bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-4 cursor-pointer hover:shadow-md hover:border-primary-500 transition-all group',
            selectedSession?.id === session.id ? 'ring-2 ring-primary-500' : ''
          ]"
        >
          <div class="flex items-start justify-between mb-3">
            <div class="flex-1 min-w-0">
              <h3 class="font-medium text-gray-800 dark:text-white truncate">{{ session.title || '未命名对话' }}</h3>
              <p class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ formatDate(session.created_at) }}</p>
            </div>
            <button
              @click.stop="deleteSession(session)"
              class="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg opacity-0 group-hover:opacity-100 transition-all"
            >
              <X class="w-4 h-4" />
            </button>
          </div>
          <div class="text-sm text-gray-600 dark:text-gray-300 line-clamp-3">
            {{ session.last_message || '暂无消息' }}
          </div>
        </div>
      </div>

      <div v-if="!sessionsData?.length" class="flex flex-col items-center justify-center h-full">
        <MessageCircle class="w-16 h-16 text-gray-300 dark:text-gray-600 mb-4" />
        <p class="text-gray-500 dark:text-gray-400">暂无历史对话</p>
        <button
          @click="router.push('/')"
          class="mt-4 px-4 py-2 text-sm bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
        >
          开始新对话
        </button>
      </div>
    </div>

    <div v-if="selectedSession" class="bg-white dark:bg-dark-800 border-t border-gray-200 dark:border-dark-600 px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="font-medium text-gray-800 dark:text-white">{{ selectedSession.title || '未命名对话' }}</h3>
          <p class="text-xs text-gray-500 dark:text-gray-400">{{ selectedSession.message_count }} 条消息</p>
        </div>
        <div class="flex gap-2">
          <button
            @click="renameSession"
            class="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            <Edit3 class="w-4 h-4 inline mr-1" />
            重命名
          </button>
          <button
            @click="viewTraces"
            class="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            <Activity class="w-4 h-4 inline mr-1" />
            链路追踪
          </button>
          <button
            @click="continueSession"
            class="px-3 py-1.5 text-sm bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
          >
            <MessageSquare class="w-4 h-4 inline mr-1" />
            继续对话
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 历史对话页面。
 *
 * 展示会话列表，支持打开会话、重命名、继续对话、删除单条与清空全部历史。
 */
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useSessions, useDeleteSession, useBatchDeleteSessions, useUpdateSession } from '@/queries/chat'
import type { Session } from '@/queries/chat'
import { Trash2, X, MessageCircle, Edit3, MessageSquare, Activity } from '@lucide/vue'
import { formatDate } from '@/utils/format'
import { useToast } from '@/composables/useToast'

const router = useRouter()
const toast = useToast()

const { data: sessionsData } = useSessions()
const deleteMutation = useDeleteSession()
const batchDeleteMutation = useBatchDeleteSessions()
const updateMutation = useUpdateSession()

const selectedSession = ref<Session | null>(null)

/** 选中指定会话以在底部展示操作栏。 */
function openSession(session: Session): void {
  selectedSession.value = session
}

function continueSession(): void {
  if (!selectedSession.value) return
  router.push(`/?session=${selectedSession.value.id}`)
  selectedSession.value = null
}

/** 跳转链路追踪页并按当前会话过滤。 */
function viewTraces(): void {
  if (!selectedSession.value) return
  router.push(`/traces?session_id=${selectedSession.value.id}`)
}

async function renameSession(): Promise<void> {
  if (!selectedSession.value) return
  const session = selectedSession.value
  try {
    const { value } = await ElMessageBox.prompt('请输入新的对话标题', '重命名对话', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: session.title || '',
      inputValidator: (v: string) => (!!v && v.trim().length > 0) || '标题不能为空'
    })
    await updateMutation.mutateAsync({ id: session.id, title: value.trim() })
    selectedSession.value = { ...session, title: value.trim() }
    toast.success('重命名成功')
  } catch (e) {
    if (typeof e === 'string' && (e === 'cancel' || e === 'close')) return
    toast.error('重命名失败，请重试')
  }
}

/** 二次确认后删除单个会话，若删除的是当前选中项则清空选中状态。 */
function deleteSession(session: Session): void {
  if (confirm(`确定要删除对话 "${session.title || '未命名对话'}" 吗？`)) {
    deleteMutation.mutate(session.id)
    if (selectedSession.value?.id === session.id) {
      selectedSession.value = null
    }
  }
}

/** 二次确认后批量删除全部会话并清空选中状态，操作不可恢复。 */
function clearAllHistory(): void {
  if (!confirm('确定要清空所有历史对话吗？此操作不可恢复。')) return
  const ids = sessionsData.value?.map(s => s.id) || []
  if (ids.length === 0) return
  batchDeleteMutation.mutate(ids)
  selectedSession.value = null
}

</script>