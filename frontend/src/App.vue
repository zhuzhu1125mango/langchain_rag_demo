<template>
  <div class="min-h-screen transition-colors duration-300">
    <div class="flex h-screen">
      <AppSidebar v-if="!isLoginPage" />
      <main class="flex-1 flex flex-col overflow-hidden">
        <!-- W6 #42：错误边界包住页面渲染树，单视图崩溃不再整页白屏 -->
        <ErrorBoundary class="flex-1 flex flex-col overflow-hidden">
          <router-view />
        </ErrorBoundary>
      </main>
    </div>
    <ToastNotification />
  </div>
</template>

<script setup lang="ts">
/**
 * 应用根组件。
 *
 * 组合左侧导航栏（AppSidebar）、主内容区（router-view，外包错误边界）与
 * 全局 Toast 通知，构建整体布局；登录/注册页不渲染侧边栏。
 * 启动时拉取当前用户信息以恢复登录态。
 */
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import AppSidebar from '@/components/AppSidebar.vue'
import ErrorBoundary from '@/components/ErrorBoundary.vue'
import ToastNotification from '@/components/ToastNotification.vue'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()
const isLoginPage = computed(() => route.name === 'Login')

onMounted(() => {
  void appStore.fetchMe()
})
</script>
