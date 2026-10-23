<template>
  <div class="flex-1 flex flex-col h-full bg-page overflow-hidden">
    <header class="bg-surface border-b border-line px-4 sm:px-6 py-3 sm:py-4">
      <div class="flex items-center justify-between gap-2">
        <div class="min-w-0">
          <h1 class="text-lg lg:text-xl font-semibold text-content truncate">历史对话</h1>
          <p class="hidden sm:block text-sm text-content-muted mt-1">查看和管理您的历史对话记录</p>
        </div>
        <AppButton
          :variant="sessionsData?.length ? 'danger' : 'secondary'"
          :disabled="!sessionsData?.length"
          class="flex-shrink-0"
          @click="clearAllHistory"
        >
          <Trash2 class="w-4 h-4" />
          <span class="hidden sm:inline">清空全部</span>
        </AppButton>
      </div>
    </header>

    <div class="flex-1 overflow-auto p-4 sm:p-6">
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        <AppCard
          v-for="session in sessionsData"
          :key="session.id"
          hoverable
          class="cursor-pointer hover:border-primary-500 group focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
          :class="selectedSession?.id === session.id ? 'ring-2 ring-primary-500' : ''"
          role="button"
          tabindex="0"
          :aria-label="`打开会话 ${session.title || '未命名对话'}`"
          @click="openSession(session)"
          @keydown.enter="openSession(session)"
          @keydown.space.prevent="openSession(session)"
        >
          <div class="flex items-start justify-between mb-3">
            <div class="flex-1 min-w-0">
              <h3 class="font-medium text-content truncate">{{ session.title || '未命名对话' }}</h3>
              <p class="text-xs text-content-muted mt-1">{{ formatDate(session.created_at) }}</p>
            </div>
            <AppButton
              variant="danger-ghost"
              size="icon"
              class="opacity-0 group-hover:opacity-100 focus-visible:opacity-100 focus:opacity-100"
              :aria-label="`删除会话 ${session.title || '未命名对话'}`"
              @click.stop="deleteSession(session)"
            >
              <X class="w-4 h-4" />
            </AppButton>
          </div>
          <div class="text-sm text-content-tertiary line-clamp-3">
            {{ session.last_message || '暂无消息' }}
          </div>
        </AppCard>
      </div>

      <AppStateView
        v-if="!sessionsData?.length"
        state="empty"
        title="暂无历史对话"
        description="开始一次对话后，这里会保存你的历史记录"
        :icon="MessageCircle"
        padding="lg"
        class="h-full"
      >
        <template #action>
          <AppButton variant="primary" @click="router.push('/')">
            开始新对话
          </AppButton>
        </template>
      </AppStateView>
    </div>

    <div v-if="selectedSession" class="bg-surface border-t border-line px-6 py-4">
      <div class="flex items-center justify-between">
        <div>
          <h3 class="font-medium text-content">{{ selectedSession.title || '未命名对话' }}</h3>
          <p class="text-xs text-content-muted">{{ selectedSession.message_count }} 条消息</p>
        </div>
        <div class="flex gap-2">
          <AppButton variant="ghost" size="nav" @click="renameSession">
            <Edit3 class="w-4 h-4" />
            重命名
          </AppButton>
          <AppButton variant="ghost" size="nav" @click="viewTraces">
            <Activity class="w-4 h-4" />
            链路追踪
          </AppButton>
          <AppButton variant="primary" size="nav" @click="continueSession">
            <MessageSquare class="w-4 h-4" />
            继续对话
          </AppButton>
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
import { AppButton, AppCard, AppStateView } from '@/components/ui'
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
async function deleteSession(session: Session): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定要删除对话 "${session.title || '未命名对话'}" 吗？`,
      '删除对话',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  deleteMutation.mutate(session.id)
  if (selectedSession.value?.id === session.id) {
    selectedSession.value = null
  }
}

/** 二次确认后批量删除全部会话并清空选中状态，操作不可恢复。 */
async function clearAllHistory(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定要清空全部 ${sessionsData.value?.length ?? 0} 条历史对话吗？此操作不可恢复。`,
      '清空历史对话',
      { confirmButtonText: '清空', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  const ids = sessionsData.value?.map(s => s.id) || []
  if (ids.length === 0) return
  batchDeleteMutation.mutate(ids)
  selectedSession.value = null
}

</script>