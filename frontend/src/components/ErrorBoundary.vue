<template>
  <slot v-if="!hasError" />
  <div v-else class="h-full min-h-screen flex items-center justify-center p-6">
    <div class="max-w-md w-full bg-white dark:bg-dark-800 border border-red-200 dark:border-red-800 rounded-2xl p-6 text-center shadow-lg">
      <AlertOctagon class="w-10 h-10 text-red-500 mx-auto mb-3" />
      <h2 class="text-base font-semibold text-gray-800 dark:text-white mb-1">页面出现异常</h2>
      <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
        渲染过程发生未预期错误，该区域已停止工作。可尝试重置恢复；若反复出现请刷新页面。
      </p>
      <pre class="text-left text-xs text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-dark-700 rounded-lg p-2 mb-4 overflow-auto max-h-32 whitespace-pre-wrap">{{ errorMessage }}</pre>
      <button
        @click="reset"
        class="px-4 py-2 text-sm bg-primary-500 text-white hover:bg-primary-600 rounded-lg transition-colors"
      >
        尝试恢复
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 全局错误边界（W6 #42）。
 *
 * onErrorCaptured 捕获子树（router-view 渲染的页面组件树）的渲染/生命周期
 * 错误，替换为兜底 UI，避免单个视图崩溃导致整页白屏；「尝试恢复」仅清除
 * 错误态重新渲染，不回滚已产生的副作用。事件处理器内未捕获的异步错误
 * 不经过此边界，由 main.ts 的 app.config.errorHandler 全局兜底。
 */
import { ref, onErrorCaptured } from 'vue'
import { AlertOctagon } from '@lucide/vue'

const hasError = ref(false)
const errorMessage = ref('')

onErrorCaptured((err, _instance, info) => {
  hasError.value = true
  errorMessage.value = `${err instanceof Error ? err.message : String(err)}\n[hook: ${info}]`
  // 完整堆栈进控制台供排障
  console.error('[ErrorBoundary] 捕获子树错误:', err, info)
  // 返回 false 阻止错误继续向上传播（避免重复触发 app.config.errorHandler）
  return false
})

function reset(): void {
  hasError.value = false
  errorMessage.value = ''
}
</script>
