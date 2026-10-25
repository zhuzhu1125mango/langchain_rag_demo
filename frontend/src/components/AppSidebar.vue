<template>
  <aside
    :inert="sidebarInert"
    :aria-hidden="sidebarInert || undefined"
    :class="[
      'fixed lg:relative inset-y-0 left-0 z-50 flex flex-col h-full transition-transform lg:transition-all duration-300',
      'w-72 max-w-[85vw] lg:max-w-none',
      appStore.sidebarCollapsed ? 'lg:w-16' : 'lg:w-64',
      mobileOpen ? 'translate-x-0' : '-translate-x-full lg:translate-x-0',
      'glass-chrome border-r'
    ]"
  >
    <div class="flex items-center justify-between h-16 px-4 shrink-0">
      <div class="flex items-center gap-3 overflow-hidden">
        <BrandMark class="w-8 h-8 flex-shrink-0" />
        <div v-show="showLabels" class="flex flex-col min-w-0 leading-tight">
          <span class="font-semibold tracking-tight text-content text-[15px] truncate">知澜</span>
          <span class="text-[10px] text-content-subtle tracking-wide truncate">企业知识问答</span>
        </div>
      </div>
      <!-- 移动端抽屉关闭按钮 -->
      <AppButton
        variant="ghost"
        size="icon"
        class="lg:hidden"
        aria-label="关闭导航菜单"
        @click="emit('close')"
      >
        <X class="w-5 h-5" />
      </AppButton>
    </div>

    <nav class="py-3 shrink-0" aria-label="主导航">
      <ul class="space-y-1 px-2">
        <li v-for="item in menuItems" :key="item.name">
          <router-link
            :to="item.path"
            @click="emit('close')"
            :title="item.label"
            :aria-current="$route.path === item.path ? 'page' : undefined"
            :class="[
              'flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-all duration-200',
              $route.path === item.path
                ? 'bg-surface text-content shadow-card ring-1 ring-line/70'
                : 'text-content-secondary hover:bg-sidebar-hover hover:text-content'
            ]"
          >
            <component
              :is="item.icon"
              class="w-5 h-5 flex-shrink-0 transition-colors"
              :class="$route.path === item.path ? 'text-primary-600 dark:text-primary-400' : 'text-content-muted'"
            />
            <span v-show="showLabels" class="truncate">{{ item.label }}</span>
          </router-link>
        </li>
      </ul>
    </nav>

    <!-- 会话历史（仅对话页展示，替代原聊天页内独立会话面板） -->
    <div
      v-if="isChatRoute && showLabels"
      class="flex-1 min-h-0 flex flex-col border-t border-line-subtle"
    >
      <div class="px-4 pt-4 pb-2 flex items-center justify-between shrink-0">
        <span class="text-[11px] font-medium uppercase tracking-wider text-content-subtle">最近对话</span>
        <AppButton
          variant="ghost"
          size="xs"
          class="text-content-tertiary hover:text-primary-600 dark:hover:text-primary-400"
          @click="handleNewSession(); emit('close')"
        >
          <Plus class="w-3 h-3" />
          新对话
        </AppButton>
      </div>
      <div class="flex-1 overflow-y-auto px-2 pb-2 space-y-0.5" aria-label="对话历史列表">
        <button
          v-for="session in sessions"
          :key="session.id"
          @click="switchSession(session); emit('close')"
          :class="[
            'w-full flex items-center gap-2 px-2.5 py-2 rounded-lg text-left text-sm transition-all',
            chatStore.currentSession?.id === session.id
              ? 'bg-surface text-content shadow-card ring-1 ring-line/70'
              : 'text-content-secondary hover:bg-sidebar-hover hover:text-content'
          ]"
          :aria-current="chatStore.currentSession?.id === session.id ? 'page' : undefined"
          :title="session.title || '未命名对话'"
        >
          <MessageSquare
            class="w-3.5 h-3.5 flex-shrink-0"
            :class="chatStore.currentSession?.id === session.id ? 'text-primary-500' : 'text-content-subtle'"
          />
          <span class="text-[13px] truncate flex-1">{{ session.title || '未命名对话' }}</span>
        </button>
        <p v-if="!sessions?.length" class="text-xs text-content-subtle text-center py-4">
          暂无会话
        </p>
      </div>
    </div>
    <div v-else class="flex-1"></div>

    <div class="p-3 border-t border-line shrink-0">
      <div class="flex items-center gap-3 px-1 py-1">
        <div class="w-8 h-8 rounded-full bg-surface-strong flex items-center justify-center text-content-secondary flex-shrink-0 ring-1 ring-line">
          <User class="w-4 h-4" />
        </div>
        <div v-show="showLabels" class="flex-1 min-w-0">
          <p class="text-sm font-medium text-content truncate">
            {{ appStore.currentUser?.username || '未登录' }}
          </p>
          <p class="text-xs text-content-muted truncate">{{ userSubtitle }}</p>
        </div>
        <AppButton
          v-show="showLabels && !!appStore.currentUser"
          variant="secondary"
          size="icon"
          class="flex-shrink-0"
          title="退出登录"
          aria-label="退出登录"
          @click="handleLogout"
        >
          <LogOut class="w-4 h-4" />
        </AppButton>
      </div>

      <div class="mt-3 flex gap-2">
        <button
          @click="appStore.toggleDark"
          :class="[
            'flex-1 p-2 rounded-lg transition-colors',
            appStore.isDark
              ? 'bg-primary-900/30 text-primary-300 hover:bg-primary-900/40'
              : 'bg-surface-subtle text-content-tertiary hover:bg-surface-hover'
          ]"
          :title="appStore.isDark ? '切换到浅色模式' : '切换到深色模式'"
          :aria-label="appStore.isDark ? '切换到浅色模式' : '切换到深色模式'"
          :aria-pressed="appStore.isDark"
        >
          <Sun v-if="!appStore.isDark" class="w-4 h-4 mx-auto" />
          <Moon v-else class="w-4 h-4 mx-auto" />
        </button>
        <button
          @click="appStore.toggleSidebar"
          class="hidden lg:block flex-1 p-2 rounded-lg bg-surface-subtle text-content-tertiary hover:bg-surface-hover-strong transition-colors"
          :title="appStore.sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
          :aria-label="appStore.sidebarCollapsed ? '展开侧边栏' : '收起侧边栏'"
          :aria-expanded="!appStore.sidebarCollapsed"
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
import type { Component } from 'vue'
import { useMediaQuery } from '@vueuse/core'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useSessions } from '@/queries/chat'
import type { Session } from '@/queries/chat'
import { api } from '@/utils/axios'
import { useToast } from '@/composables/useToast'
import { AppButton } from '@/components/ui'
import BrandMark from '@/components/BrandMark.vue'
import { MessageCircle, FolderOpen, Settings, User, Sun, Moon, ChevronLeft, ChevronRight, Clock, LogOut, Activity, MessageSquare, Plus, X } from '@lucide/vue'

const props = defineProps<{
  /** 移动端抽屉是否展开（桌面端忽略，始终按折叠态宽度展示） */
  mobileOpen?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

const appStore = useAppStore()
const chatStore = useChatStore()
const route = useRoute()
const router = useRouter()
const toast = useToast()

// 移动端抽屉展开时始终显示文字（不受桌面折叠态影响）
const showLabels = computed(() => Boolean(props.mobileOpen) || !appStore.sidebarCollapsed)

// 移动端抽屉关闭时禁用交互/屏读（避免键盘 focus 落到屏外元素）
const isDesktop = useMediaQuery('(min-width: 1024px)')
const sidebarInert = computed(() => !isDesktop.value && !props.mobileOpen)

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
  /** 图标组件（W6 #55：lucide 图标均为 Vue 组件，用 Component 替代 unknown） */
  icon: Component
}

const menuItems: MenuItem[] = [
  { name: 'chat', path: '/', label: '对话', icon: MessageCircle },
  { name: 'knowledge-base', path: '/knowledge-base', label: '知识库管理', icon: FolderOpen },
  { name: 'history', path: '/history', label: '历史对话', icon: Clock },
  { name: 'traces', path: '/traces', label: '链路追踪', icon: Activity },
  { name: 'settings', path: '/settings', label: '设置中心', icon: Settings }
]
</script>
