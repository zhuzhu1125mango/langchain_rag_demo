<template>
  <div class="min-h-screen transition-colors duration-300">
    <div class="flex h-screen">
      <AppSidebar />
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
 * 构建整体布局。
 */
import AppSidebar from '@/components/AppSidebar.vue'
import ToastNotification from '@/components/ToastNotification.vue'
</script>