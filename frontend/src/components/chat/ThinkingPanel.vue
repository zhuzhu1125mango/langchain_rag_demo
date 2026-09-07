<template>
  <div
    v-if="hasThinking"
    class="rounded-xl border transition-all duration-200 overflow-hidden"
    :class="[
      isExpanded
        ? 'bg-gray-50/80 border-gray-200 dark:bg-dark-700/60 dark:border-dark-600'
        : 'bg-gray-50 border-gray-200 hover:bg-gray-100 dark:bg-dark-700/40 dark:border-dark-600 dark:hover:bg-dark-700/60'
    ]"
  >
    <!-- 折叠态头部：摘要 + 展开按钮 -->
    <button
      v-if="!isExpanded"
      type="button"
      class="w-full flex items-center justify-between gap-2 px-3 py-2 text-left"
      @click="toggle"
    >
      <div class="flex items-center gap-2 min-w-0">
        <Loader2
          v-if="loading"
          class="w-3.5 h-3.5 text-blue-500 dark:text-blue-400 flex-shrink-0 animate-spin"
        />
        <Brain
          v-else
          class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400 flex-shrink-0"
        />
        <span class="text-xs text-gray-700 dark:text-gray-200 truncate">
          {{ summaryText }}
        </span>
      </div>
      <ChevronDown class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
    </button>

    <!-- 展开态：思考文本流式滚动 -->
    <div v-else class="px-3 py-2">
      <div class="flex items-center justify-between gap-2 mb-1.5">
        <div class="flex items-center gap-2">
          <Brain class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
          <span class="text-xs font-medium text-gray-700 dark:text-gray-200">深度思考</span>
          <Loader2
            v-if="loading"
            class="w-3 h-3 text-blue-500 dark:text-blue-400 animate-spin"
          />
        </div>
        <button
          type="button"
          class="p-1 rounded hover:bg-gray-200 dark:hover:bg-dark-600 transition-colors"
          @click="toggle"
        >
          <ChevronUp class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        </button>
      </div>

      <div
        ref="contentRef"
        class="max-h-56 overflow-y-auto text-xs text-gray-600 dark:text-gray-300 leading-relaxed whitespace-pre-wrap break-words"
      >{{ thinking }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 模型深度思考折叠面板组件
 * @description 以 DeepSeek 风格展示模型原始思考内容：思考流式期间默认展开并自动滚动，
 * 正文开始输出后自动折叠；用户手动展开/折叠后以用户操作为准，不再自动切换。
 *
 * @props thinking - 模型原始思考文本（流式增量累积）
 * @props loading - 消息是否处于流式接收中（思考阶段为 true，正文开始后为 false）
 */
import { ref, computed, watch, nextTick } from 'vue'
import { Brain, ChevronDown, ChevronUp, Loader2 } from '@lucide/vue'

const props = defineProps<{
  thinking?: string
  loading?: boolean
}>()

const isExpanded = ref(true)
// 用户手动切换后不再自动展开/折叠
const userToggled = ref(false)
const contentRef = ref<HTMLElement | null>(null)

const hasThinking = computed(() => !!props.thinking?.trim())

const summaryText = computed(() => {
  return props.loading ? '正在深度思考...' : '已深度思考'
})

/** 用户手动切换：记录标记，之后不再自动切换 */
function toggle(): void {
  userToggled.value = true
  isExpanded.value = !isExpanded.value
}

// 流式阶段保持展开；正文开始（loading 置 false）后自动折叠，除非用户已手动切换
watch(
  () => props.loading,
  (loading, prev) => {
    if (userToggled.value || prev === undefined) return
    if (!loading) {
      isExpanded.value = false
    }
  }
)

// 展开且思考内容增长时自动滚动到底部
watch(
  () => props.thinking,
  async () => {
    if (!isExpanded.value) return
    await nextTick()
    const el = contentRef.value
    if (el) el.scrollTop = el.scrollHeight
  }
)
</script>
