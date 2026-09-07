<template>
  <div
    v-if="hasReasoning"
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
      @click="isExpanded = true"
    >
      <div class="flex items-center gap-2 min-w-0">
        <Loader2
          v-if="isRunning"
          class="w-3.5 h-3.5 text-blue-500 dark:text-blue-400 flex-shrink-0 animate-spin"
        />
        <Sparkles
          v-else
          class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400 flex-shrink-0"
        />
        <span class="text-xs text-gray-700 dark:text-gray-200 truncate">
          {{ summaryText }}
        </span>
      </div>
      <ChevronDown class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
    </button>

    <!-- 展开态：时间线 -->
    <div v-else class="px-3 py-2">
      <div class="flex items-center justify-between gap-2 mb-2">
        <div class="flex items-center gap-2">
          <Sparkles class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
          <span class="text-xs font-medium text-gray-700 dark:text-gray-200">思考过程</span>
        </div>
        <button
          type="button"
          class="p-1 rounded hover:bg-gray-200 dark:hover:bg-dark-600 transition-colors"
          @click="isExpanded = false"
        >
          <ChevronUp class="w-3.5 h-3.5 text-gray-400" />
        </button>
      </div>

      <div class="space-y-0">
        <div
          v-for="(step, index) in sortedSteps"
          :key="step.id"
          class="flex gap-2.5"
        >
          <!-- 时间线 -->
          <div class="flex flex-col items-center pt-0.5">
            <div
              class="w-4 h-4 rounded-full flex items-center justify-center"
              :class="getStatusDotClass(step.status)"
            >
              <Loader2
                v-if="step.status === 'running'"
                class="w-2.5 h-2.5 text-blue-600 dark:text-blue-300"
              />
              <Check
                v-else-if="step.status === 'done'"
                class="w-2.5 h-2.5 text-green-600 dark:text-green-300"
              />
              <X
                v-else-if="step.status === 'failed'"
                class="w-2.5 h-2.5 text-red-600 dark:text-red-300"
              />
            </div>
            <div
              v-if="index < sortedSteps.length - 1"
              class="w-px flex-1 min-h-[16px] my-0.5"
              :class="step.status === 'failed' ? 'bg-red-200 dark:bg-red-900/40' : 'bg-gray-200 dark:bg-dark-600'"
            />
          </div>

          <!-- 内容 -->
          <div class="flex-1 pb-3 min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-xs font-medium text-gray-800 dark:text-gray-100">
                {{ step.title }}
              </span>
              <span
                v-if="step.duration_ms && step.duration_ms > 0"
                class="text-xs text-gray-400 dark:text-gray-500"
              >
                {{ step.duration_ms }}ms
              </span>
            </div>
            <p
              v-if="step.content"
              class="text-xs text-gray-600 dark:text-gray-300 mt-0.5 leading-relaxed"
            >
              {{ step.content }}
            </p>
            <p
              v-if="step.metadata?.sources_count !== undefined"
              class="text-xs text-gray-500 dark:text-gray-400 mt-0.5"
            >
              来源数：{{ step.metadata.sources_count }}
            </p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 推理/搜索过程折叠面板组件
 * @description 以豆包/DeepSeek 风格展示模型的思考与搜索过程，默认折叠显示摘要，
 * 点击后展开为时间线。支持加载中、成功、失败三种状态，自动按时间戳排序。
 *
 * @props steps - 当前消息的 reasoning 步骤列表
 */
import { ref, computed } from 'vue'
import { Sparkles, ChevronDown, ChevronUp, Loader2, Check, X } from '@lucide/vue'
import type { ReasoningStep } from '@/queries/chat'

const props = defineProps<{
  steps?: ReasoningStep[]
}>()

const isExpanded = ref(false)

const hasReasoning = computed(() => {
  return Array.isArray(props.steps) && props.steps.length > 0
})

/** 按 step 去重：同一阶段保留最后出现的事件（与后端"新事件替换旧事件"语义一致）。
 * 历史消息的 reasoning 数组可能仍存有逐事件累积的重复条目（如 answer_generate 的
 * running + done 两条），不去重会导致残留 running 状态使 spinner 一直转圈。 */
const dedupedSteps = computed(() => {
  if (!props.steps) return []
  const map = new Map<string, ReasoningStep>()
  props.steps.forEach(s => map.set(s.step, s))
  return Array.from(map.values())
})

/** 按时间戳排序的步骤列表（未提供时间戳的排在最后） */
const sortedSteps = computed(() => {
  return [...dedupedSteps.value].sort((a, b) => {
    const ta = a.timestamp ?? Number.MAX_SAFE_INTEGER
    const tb = b.timestamp ?? Number.MAX_SAFE_INTEGER
    return ta - tb
  })
})

/** 是否存在运行中的步骤 */
const isRunning = computed(() => {
  return sortedSteps.value.some(s => s.status === 'running')
})

/** 已完成/失败步骤数量 */
const finishedCount = computed(() => {
  return sortedSteps.value.filter(s => s.status === 'done' || s.status === 'failed').length
})

/** 折叠态摘要文本 */
const summaryText = computed(() => {
  const parts: string[] = []
  const titles: string[] = []

  sortedSteps.value.forEach(step => {
    if (step.status === 'running') {
      titles.push(`正在${step.title}`)
    } else if (step.status === 'done' && !titles.includes(step.title)) {
      titles.push(`已${step.title}`)
    }
  })

  if (titles.length > 0) {
    parts.push(titles.join(' · '))
  }

  const webStep = sortedSteps.value.find(s => s.step === 'web_search')
  if (webStep?.status === 'done' && webStep.metadata?.sources_count !== undefined) {
    parts.push(`已搜索 ${webStep.metadata.sources_count} 个网页`)
  }

  const kbStep = sortedSteps.value.find(s => s.step === 'kb_retrieve')
  if (kbStep?.status === 'done' && kbStep.metadata?.sources_count !== undefined) {
    parts.push(`命中 ${kbStep.metadata.sources_count} 个片段`)
  }

  if (parts.length === 0) {
    return isRunning.value ? '正在思考...' : `已执行 ${finishedCount.value} 个步骤`
  }

  return parts.join(' · ')
})

/** 根据状态获取时间线节点样式类 */
function getStatusDotClass(status: ReasoningStep['status']): string {
  switch (status) {
    case 'running':
      return 'bg-blue-100 dark:bg-blue-900/40'
    case 'done':
      return 'bg-green-100 dark:bg-green-900/40'
    case 'failed':
      return 'bg-red-100 dark:bg-red-900/40'
    default:
      return 'bg-gray-100 dark:bg-dark-600'
  }
}
</script>
