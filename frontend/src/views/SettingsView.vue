<template>
  <div class="flex flex-col lg:flex-row h-full">
    <aside class="w-full lg:w-60 shrink-0 glass-subtle border-b lg:border-b-0 lg:border-r p-2 lg:p-3">
      <nav class="flex lg:block gap-1 overflow-x-auto lg:space-y-1">
        <h2 class="hidden lg:block px-3 py-2 text-[11px] font-semibold text-content-subtle uppercase tracking-wider">设置菜单</h2>
        <button
          v-for="item in menuItems"
          :key="item.name"
          @click="navigateTo(item.path)"
          :aria-current="route.path === item.path ? 'page' : undefined"
          :class="[
            'flex-shrink-0 whitespace-nowrap w-auto lg:w-full flex items-center gap-3 px-3 py-2 rounded-lg transition-all duration-200 text-left text-sm font-medium',
            route.path === item.path
              ? 'bg-surface text-content shadow-card ring-1 ring-line/70'
              : 'text-content-secondary hover:bg-sidebar-hover hover:text-content'
          ]"
        >
          <component
            :is="item.icon"
            class="w-5 h-5"
            :class="route.path === item.path ? 'text-primary-600 dark:text-primary-400' : 'text-content-muted'"
          />
          <span>{{ item.label }}</span>
        </button>
      </nav>
    </aside>
    <main class="flex-1 overflow-auto min-w-0">
      <router-view />
    </main>
  </div>
</template>

<script setup lang="ts">
/**
 * 设置页面容器，含侧边导航。
 *
 * 左侧渲染设置菜单，右侧通过 router-view 展示当前子页面
 * （常规/分类/标签/API密钥/学习引擎/A/B实验/反馈统计）。
 */
import { useRoute, useRouter } from 'vue-router'
import type { Component } from 'vue'
import { Settings, FolderOpen, Tags, Cpu, BarChart3, FlaskConical, KeyRound } from '@lucide/vue'

const route = useRoute()
const router = useRouter()

interface MenuItem {
  name: string
  path: string
  label: string
  /** 图标组件（W6 #55：lucide 图标均为 Vue 组件，用 Component 替代 unknown） */
  icon: Component
}

const menuItems: MenuItem[] = [
  { name: 'general', path: '/settings', label: '常规设置', icon: Settings },
  { name: 'categories', path: '/settings/categories', label: '分类管理', icon: FolderOpen },
  { name: 'tags', path: '/settings/tags', label: '标签管理', icon: Tags },
  { name: 'api-keys', path: '/settings/api-keys', label: 'API 密钥', icon: KeyRound },
  { name: 'learning', path: '/settings/learning', label: '学习引擎', icon: Cpu },
  { name: 'experiments', path: '/settings/experiments', label: 'A/B实验', icon: FlaskConical },
  { name: 'feedback', path: '/settings/feedback', label: '反馈统计', icon: BarChart3 }
]

/** 点击菜单项跳转到对应设置子路由。 */
function navigateTo(path: string): void {
  router.push(path)
}
</script>
