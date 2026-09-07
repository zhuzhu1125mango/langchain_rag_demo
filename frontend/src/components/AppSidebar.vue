<template>
  <aside 
    :class="[
      'fixed lg:relative z-50 flex flex-col transition-all duration-300',
      appStore.sidebarCollapsed ? 'w-16' : 'w-64',
      'bg-white dark:bg-dark-800 border-r border-gray-200 dark:border-dark-600'
    ]"
  >
    <div class="flex items-center h-16 px-4 border-b border-gray-200 dark:border-dark-600 shrink-0">
      <div class="flex items-center gap-3 overflow-hidden">
        <div class="w-8 h-8 rounded-lg bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center flex-shrink-0">
          <BookOpen class="w-5 h-5 text-white" />
        </div>
        <span 
          v-show="!appStore.sidebarCollapsed" 
          class="font-bold text-gray-800 dark:text-white text-lg truncate"
        >
          RAG 知识库
        </span>
      </div>
    </div>

    <nav class="py-3 shrink-0">
      <ul class="space-y-1 px-2">
        <li v-for="item in menuItems" :key="item.name">
          <router-link
            :to="item.path"
            :class="[
              'flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200',
              'hover:bg-gray-100 dark:hover:bg-dark-700',
              $route.path === item.path 
                ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/30 dark:text-primary-400' 
                : 'text-gray-600 dark:text-gray-300'
            ]"
          >
            <component :is="item.icon" class="w-5 h-5 flex-shrink-0" />
            <span v-show="!appStore.sidebarCollapsed" class="truncate">{{ item.label }}</span>
          </router-link>
        </li>
      </ul>
    </nav>

    <!-- 会话历史（仅对话页展示，替代原聊天页内独立会话面板） -->
    <div
      v-if="isChatRoute && !appStore.sidebarCollapsed"
      class="flex-1 min-h-0 flex flex-col border-t border-gray-100 dark:border-dark-700"
    >
      <div class="px-4 pt-3 pb-1.5 flex items-center justify-between shrink-0">
        <span class="text-xs font-medium text-gray-400 dark:text-gray-500">对话历史</span>
        <button
          @click="handleNewSession"
          class="flex items-center gap-1 px-2 py-1 text-xs text-primary-600 dark:text-primary-400 rounded-lg hover:bg-primary-50 dark:hover:bg-primary-900/30 transition-colors"
        >
          <Plus class="w-3 h-3" />
          新对话
        </button>
      </div>
      <div class="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5">
        <button
          v-for="session in sessions"
          :key="session.id"
          @click="switchSession(session)"
          :class="[
            'w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left transition-colors',
            chatStore.currentSession?.id === session.id
              ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400'
              : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700'
          ]"
          :title="session.title || '未命名对话'"
        >
          <MessageSquare class="w-3.5 h-3.5 flex-shrink-0 opacity-60" />
          <span class="text-sm truncate flex-1">{{ session.title || '未命名对话' }}</span>
        </button>
        <p v-if="!sessions?.length" class="text-xs text-gray-400 dark:text-gray-500 text-center py-4">
          暂无会话
        </p>
      </div>
    </div>
    <div v-else class="flex-1"></div>

    <div class="p-4 border-t border-gray-200 dark:border-dark-600 shrink-0">
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white font-medium flex-shrink-0">
          <User class="w-5 h-5" />
        </div>
        <div v-show="!appStore.sidebarCollapsed" class="flex-1 min-w-0">
          <p class="text-sm font-medium text-gray-800 dark:text-white truncate">
            {{ appStore.currentUser?.username || '未登录' }}
          </p>
          <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ userSubtitle }}</p>
        </div>
        <button
          v-show="!appStore.sidebarCollapsed && !!appStore.currentUser"
          @click="handleLogout"
          class="p-2 rounded-lg bg-gray-100 dark:bg-dark-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-dark-600 transition-colors flex-shrink-0"
          title="退出登录"
        >
          <LogOut class="w-4 h-4" />
        </button>
      </div>

      <div class="mt-3 flex gap-2">
        <button
          @click="appStore.toggleDark"
          :class="[
            'flex-1 p-2 rounded-lg transition-colors',
            appStore.isDark ? 'bg-gray-700 text-gray-300' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
          ]"
          :title="appStore.isDark ? '切换到浅色模式' : '切换到深色模式'"
        >
          <Sun v-if="!appStore.isDark" class="w-4 h-4 mx-auto" />
          <Moon v-else class="w-4 h-4 mx-auto" />
        </button>
        <button
          @click="appStore.toggleSidebar"
          class="flex-1 p-2 rounded-lg bg-gray-100 dark:bg-dark-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-dark-600 transition-colors"
          :title="appStore.sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
        >
          <ChevronLeft v-if="!appStore.sidebarCollapsed" class="w-4 h-4 mx-auto" />
          <ChevronRight v-else class="w-4 h-4 mx-auto" />
        </button>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 应用侧边栏导航组件
 * @description 全局左侧导航栏：应用标识、主导航菜单（对话/知识库/历史/设置）、
 * 对话历史列表（仅对话页展示，支持切换与新建会话）、当前用户与退出登录、
 * 主题切换与侧边栏收起/展开。折叠态宽度 64px，展开态宽度 256px。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useSessions } from '@/queries/chat'
import type { Session } from '@/queries/chat'
import { api } from '@/utils/axios'
import { useToast } from '@/composables/useToast'
import { BookOpen, MessageCircle, FolderOpen, Settings, User, Sun, Moon, ChevronLeft, ChevronRight, Clock, LogOut, Activity, MessageSquare, Plus } from '@lucide/vue'

const appStore = useAppStore()
const chatStore = useChatStore()
const route = useRoute()
const router = useRouter()
const toast = useToast()

// 与 ChatView 共享同一 vue-query 缓存（queryKey: ['sessions']）
const { data: sessions } = useSessions()

const userSubtitle = computed(() =>
  appStore.currentUser ? `ID: ${appStore.currentUser.id.slice(0, 8)}` : '本地模式'
)

const isChatRoute = computed(() => route.path === '/')

/** 退出登录：清空本地 token 并跳转登录页。 */
function handleLogout() {
  appStore.logout()
  router.push('/login')
}

/** 切换会话：仅更新路由，由 ChatView 监听 query.session 完成历史加载。 */
function switchSession(session: Session): void {
  if (chatStore.currentSession?.id === session.id) return
  router.replace({ path: '/', query: { session: session.id } })
}

/** 新建会话并切换到该会话。 */
async function handleNewSession(): Promise<void> {
  try {
    const res = await api.post<{ id: string; title: string }>('/sessions/', { title: '新会话' })
    if (res && res.id) {
      chatStore.setCurrentSession({
        id: res.id,
        title: res.title,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString()
      })
      chatStore.clearMessages()
      router.replace({ path: '/', query: { session: res.id } })
    }
  } catch (error) {
    console.error('创建会话失败:', error)
    toast.error('创建会话失败', '请稍后重试')
  }
}

/** 主导航菜单项定义 */
interface MenuItem {
  /** 菜单项唯一标识（同时作为路由 name） */
  name: string
  /** 路由路径 */
  path: string
  /** 显示文本 */
  label: string
  /** 图标组件 */
  icon: unknown
}

const menuItems: MenuItem[] = [
  { name: 'chat', path: '/', label: '对话', icon: MessageCircle },
  { name: 'knowledge-base', path: '/knowledge-base', label: '知识库管理', icon: FolderOpen },
  { name: 'history', path: '/history', label: '历史对话', icon: Clock },
  { name: 'traces', path: '/traces', label: '链路追踪', icon: Activity },
  { name: 'settings', path: '/settings', label: '设置中心', icon: Settings }
]
</script>
