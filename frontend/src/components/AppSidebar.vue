<template>
  <aside 
    :class="[
      'fixed lg:relative z-50 flex flex-col transition-all duration-300',
      appStore.sidebarCollapsed ? 'w-16' : 'w-64',
      'bg-white dark:bg-dark-800 border-r border-gray-200 dark:border-dark-600'
    ]"
  >
    <div class="flex items-center h-16 px-4 border-b border-gray-200 dark:border-dark-600">
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

    <nav class="flex-1 py-4 overflow-y-auto">
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

    <div class="p-4 border-t border-gray-200 dark:border-dark-600">
      <div class="flex items-center gap-3">
        <div class="w-9 h-9 rounded-full bg-gradient-to-br from-primary-400 to-primary-600 flex items-center justify-center text-white font-medium flex-shrink-0">
          <User class="w-5 h-5" />
        </div>
        <div v-show="!appStore.sidebarCollapsed" class="flex-1 min-w-0">
          <p class="text-sm font-medium text-gray-800 dark:text-white truncate">管理员</p>
          <p class="text-xs text-gray-500 dark:text-gray-400 truncate">admin@example.com</p>
        </div>
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
 * @description 全局左侧导航栏，展示应用标识、主导航菜单（对话/知识库/历史/设置）、
 * 用户信息以及主题切换、侧边栏收起/展开按钮。当前路由对应菜单项高亮。
 * 折叠态宽度 64px，展开态宽度 256px，由 appStore.sidebarCollapsed 控制。
 */
import { useAppStore } from '@/stores/app'
import { BookOpen, MessageCircle, FolderOpen, Settings, User, Sun, Moon, ChevronLeft, ChevronRight, Clock } from '@lucide/vue'

const appStore = useAppStore()

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
  { name: 'settings', path: '/settings', label: '设置中心', icon: Settings }
]
</script>