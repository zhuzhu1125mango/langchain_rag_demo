<template>
  <div class="min-h-screen transition-colors duration-300">
    <!-- UX-3：键盘用户首个 Tab 可跳过导航直达主内容 -->
    <a href="#main-content" class="skip-link">跳到主要内容</a>
    <div class="flex h-screen">
      <AppSidebar
        v-if="!isLoginPage"
        :mobile-open="mobileNavOpen"
        @close="mobileNavOpen = false"
      />
      <!-- 移动端导航遮罩：点击关闭抽屉 -->
      <div
        v-if="!isLoginPage && mobileNavOpen"
        class="fixed inset-0 z-40 bg-black/50 lg:hidden"
        aria-hidden="true"
        @click="mobileNavOpen = false"
      ></div>
      <main id="main-content" tabindex="-1" class="flex-1 flex flex-col overflow-hidden min-w-0">
        <!-- 移动端顶栏：汉堡菜单（桌面端隐藏） -->
        <div
          v-if="!isLoginPage"
          class="lg:hidden flex items-center gap-2 h-12 px-3 border-b border-gray-200 dark:border-dark-600 bg-white dark:bg-dark-800 shrink-0"
        >
          <button
            @click="mobileNavOpen = true"
            class="-ml-1 p-2 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 transition-colors"
            aria-label="打开导航菜单"
          >
            <Menu class="w-5 h-5" />
          </button>
          <span class="font-semibold text-sm text-gray-800 dark:text-white">RAG 知识库</span>
        </div>
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
 * 移动端（<lg）：侧栏改为抽屉 + 顶栏汉堡菜单，主区不再被固定宽度挤压。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Menu } from '@lucide/vue'
import AppSidebar from '@/components/AppSidebar.vue'
import ErrorBoundary from '@/components/ErrorBoundary.vue'
import ToastNotification from '@/components/ToastNotification.vue'
import { useAppStore } from '@/stores/app'

const route = useRoute()
const appStore = useAppStore()
const isLoginPage = computed(() => route.name === 'Login')
// 移动端抽屉开关（桌面端不生效）
const mobileNavOpen = ref(false)

onMounted(() => {
  void appStore.fetchMe()
})
</script>
