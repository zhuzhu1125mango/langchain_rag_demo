<template>
  <div class="flex h-full">
    <aside class="w-64 bg-white dark:bg-dark-800 border-r border-gray-200 dark:border-dark-600 p-4">
      <nav class="space-y-1">
        <h2 class="px-3 py-2 text-xs font-semibold text-gray-400 uppercase tracking-wider">设置菜单</h2>
        <button
          v-for="item in menuItems"
          :key="item.name"
          @click="navigateTo(item.path)"
          :class="[
            'w-full flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200 text-left',
            'hover:bg-gray-100 dark:hover:bg-dark-700',
            route.path === item.path
              ? 'bg-primary-50 text-primary-600 dark:bg-primary-900/30 dark:text-primary-400'
              : 'text-gray-600 dark:text-gray-300'
          ]"
        >
          <component :is="item.icon" class="w-5 h-5" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
    </aside>
    <main class="flex-1 overflow-auto">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
/**
 * 设置页面容器，含侧边导航。
 *
 * 左侧渲染设置菜单，右侧通过 router-view 展示当前子页面
 * （常规/分类/标签/学习引擎/A/B实验/反馈统计）。
 */
import { useRoute, useRouter } from 'vue-router'
import { Settings, FolderOpen, Tags, Cpu, BarChart3, FlaskConical } from '@lucide/vue'

const route = useRoute()
const router = useRouter()

interface MenuItem {
  name: string
  path: string
  label: string
  icon: unknown
}

const menuItems: MenuItem[] = [
  { name: 'general', path: '/settings', label: '常规设置', icon: Settings },
  { name: 'categories', path: '/settings/categories', label: '分类管理', icon: FolderOpen },
  { name: 'tags', path: '/settings/tags', label: '标签管理', icon: Tags },
  { name: 'learning', path: '/settings/learning', label: '学习引擎', icon: Cpu },
  { name: 'experiments', path: '/settings/experiments', label: 'A/B实验', icon: FlaskConical },
  { name: 'feedback', path: '/settings/feedback', label: '反馈统计', icon: BarChart3 }
]

/** 点击菜单项跳转到对应设置子路由。 */
function navigateTo(path: string): void {
  router.push(path)
}
</script>
