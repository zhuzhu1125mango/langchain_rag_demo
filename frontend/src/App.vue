<template>
  <div class="min-h-screen transition-colors duration-300">
    <div class="flex h-screen">
      <AppSidebar v-if="!isLoginPage" />
      <main class="flex-1 flex flex-col overflow-hidden">
        <router-view />
      </main>
    </div>
    <ToastNotification />
  </div>
</template>

<script setup lang="ts">
/**
 * 应用根组件。
 *
 * 组合左侧导航栏（AppSidebar）、主内容区（router-view）与全局 Toast 通知，
 * 构建整体布局；登录/注册页不渲染侧边栏。启动时拉取当前用户信息以恢复登录态。
 */
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from '@/components/AppSidebar.vue'
import ToastNotification from '@/components/ToastNotification.vue'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()
const isLoginPage = computed(() => route.name === 'Login')

onMounted(() => {
  void appStore.fetchMe()
})
</script>
