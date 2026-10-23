<template>
  <div class="px-3 sm:px-6 pb-3 sm:pb-4 pt-1">
    <div class="max-w-4xl mx-auto">
      <!-- 问题重写结果显示 -->
      <div v-if="rewriteResult" class="mb-2 px-3 py-2 bg-primary-50/60 dark:bg-primary-900/20 border border-primary-100 dark:border-primary-800/60 rounded-xl">
        <div class="flex items-start justify-between gap-2">
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-1.5 mb-1">
              <Sparkles class="w-3.5 h-3.5 text-primary-500" />
              <span class="text-xs font-medium text-primary-600 dark:text-primary-400">问题已优化</span>
            </div>
            <p class="text-sm text-content-secondary truncate">{{ rewriteResult.rewritten }}</p>
            <p v-if="rewriteResult.changes && rewriteResult.changes !== '问题无需修改'" class="text-xs text-content-subtle mt-0.5 truncate">
              {{ rewriteResult.changes }}
            </p>
          </div>
          <div class="flex items-center gap-1.5 flex-shrink-0">
            <button
              @click="emit('use-rewritten-question')"
              class="px-2.5 py-1 text-xs bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
            >
              采用
            </button>
            <button
              @click="emit('clear-rewrite')"
              class="p-1 text-gray-400 hover:text-content-tertiary transition-colors"
            >
              <X class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>

      <!-- 问题分类标签（单行轻量 chip） -->
      <div v-if="questionClassification && !rewriteResult" class="mb-2 flex items-center gap-2">
        <span :class="[
          'px-2 py-0.5 text-xs rounded-full font-medium',
          questionClassification.type === 'FACTUAL' ? 'bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400' :
          questionClassification.type === 'OPINION' ? 'bg-purple-50 dark:bg-purple-900/30 text-purple-600 dark:text-purple-400' :
          questionClassification.type === 'OPERATIONAL' ? 'bg-green-50 dark:bg-green-900/30 text-green-600 dark:text-green-400' :
          questionClassification.type === 'EXPLANATORY' ? 'bg-orange-50 dark:bg-orange-900/30 text-orange-600 dark:text-orange-400' :
          questionClassification.type === 'COMPARATIVE' ? 'bg-pink-50 dark:bg-pink-900/30 text-pink-600 dark:text-pink-400' :
          'bg-surface-subtle text-content-tertiary'
        ]">
          {{ getQuestionTypeLabel(questionClassification.type) }}
        </span>
        <span v-if="questionClassification.subtype" class="text-xs text-content-subtle">
          {{ questionClassification.subtype }}
        </span>
      </div>

      <!-- 知识库推荐（轻量 chips 行） -->
      <div v-if="kbRecommendations.length > 0 && !rewriteResult" class="mb-2 flex items-center gap-1.5 flex-wrap">
        <span class="flex items-center gap-1 text-xs text-content-subtle">
          <Lightbulb class="w-3.5 h-3.5" />
          推荐
        </span>
        <button
          v-for="rec in kbRecommendations"
          :key="rec.kb_id"
          @click="emit('toggle-kb-id', rec.kb_id)"
          :class="[
            'px-2.5 py-1 text-xs rounded-full flex items-center gap-1 transition-colors',
            selectedKbs.includes(rec.kb_id)
              ? 'bg-primary-500 text-white'
              : 'bg-surface text-content-tertiary border border-line hover:border-primary-300 dark:hover:border-primary-700'
          ]"
        >
          <Database class="w-3 h-3" />
          <span>{{ getKbName(rec.kb_id) }}</span>
          <span class="opacity-60">{{ rec.relevance_score.toFixed(2) }}</span>
        </button>
      </div>

      <!-- 智能推荐问题（紧凑行） -->
      <div v-if="suggestions.length > 0 || isGeneratingSuggestions" class="mb-2 flex items-center gap-1.5 flex-wrap">
        <span class="flex items-center gap-1 text-xs text-content-subtle">
          <Sparkles class="w-3.5 h-3.5" />
          猜你想问
        </span>
        <span v-if="isGeneratingSuggestions" class="flex gap-0.5 inline-flex items-center">
          <span class="w-1 h-1 bg-primary-400 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
          <span class="w-1 h-1 bg-primary-400 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
          <span class="w-1 h-1 bg-primary-400 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
        </span>
        <button
          v-for="(suggestion, index) in suggestions"
          :key="index"
          @click="emit('send-quick-question', suggestion)"
          class="px-2.5 py-1 text-xs text-content-tertiary bg-surface-subtle/80 rounded-full hover:bg-surface-hover-strong transition-colors max-w-[280px] truncate"
        >
          {{ suggestion }}
        </button>
      </div>

      <!-- 主输入卡片：无边框输入区 + 底部工具条 -->
      <div class="bg-surface border border-line rounded-2xl shadow-sm focus-within:border-primary-400 dark:focus-within:border-primary-600 transition-colors">
        <textarea
          ref="textareaRef"
          v-model="questionInput"
          @keydown.enter.exact.prevent="onSend"
          @input="onInput"
          placeholder="输入您的问题，Enter 发送"
          aria-label="输入问题"
          rows="1"
          class="w-full px-4 pt-3 pb-1 bg-transparent text-base sm:text-[15px] text-content placeholder-gray-400 dark:placeholder-gray-500 rounded-t-2xl resize-none focus:outline-none"
          :maxlength="maxLength"
        ></textarea>

        <div class="flex flex-wrap items-center justify-between gap-2 px-3 pb-2.5 pt-1">
          <!-- 左侧：功能开关 pills（深度思考在前，联网搜索在后） -->
          <div class="flex items-center gap-1.5">
            <button
              @click="emit('toggle-deep-thinking')"
              :title="deepThinkingTitle"
              :class="[
                'flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-full transition-colors',
                deepThinking
                  ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400'
                  : 'text-content-muted hover:bg-surface-hover'
              ]"
            >
              <Brain class="w-3.5 h-3.5" />
              <span>{{ deepThinkingLabel }}</span>
            </button>
            <button
              @click="emit('toggle-web-search')"
              :title="useWebSearch ? '关闭联网搜索' : '开启联网搜索'"
              :class="[
                'flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-full transition-colors',
                useWebSearch
                  ? 'bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400'
                  : 'text-content-muted hover:bg-surface-hover'
              ]"
            >
              <Globe class="w-3.5 h-3.5" />
              <span>{{ webSearchLabel }}</span>
              <span
                class="w-1.5 h-1.5 rounded-full"
                :class="webSearchDotClass"
              ></span>
            </button>
          </div>

          <!-- 右侧：优化问题 / 字数 / 发送 -->
          <div class="flex items-center gap-2">
            <button
              v-if="questionInput.trim() && !rewriteResult"
              @click="emit('rewrite-question')"
              :disabled="isRewritingQuestion"
              class="p-1.5 text-gray-400 hover:text-primary-500 transition-colors disabled:opacity-50"
              title="优化问题"
              aria-label="优化问题"
            >
              <Loader2 v-if="isRewritingQuestion" class="w-4 h-4 animate-spin" />
              <Sparkles v-else class="w-4 h-4" />
            </button>
            <span
              :class="[
                'hidden sm:inline text-xs tabular-nums',
                questionInput.length >= maxLength ? 'text-red-500' : 'text-content-disabled'
              ]"
            >
              {{ questionInput.length }}/{{ maxLength }}
            </span>
            <button
              v-if="isTyping"
              @click="emit('stop')"
              class="w-8 h-8 rounded-full flex items-center justify-center bg-content text-surface hover:bg-content-strong transition-all"
              title="停止生成"
              aria-label="停止生成"
            >
              <Square class="w-3 h-3 fill-current" />
            </button>
            <button
              v-else
              @click="onSend"
              :disabled="!questionInput.trim()"
              :class="[
                'w-8 h-8 rounded-full flex items-center justify-center transition-all',
                questionInput.trim()
                  ? 'bg-primary-500 text-white hover:bg-primary-600 shadow-sm shadow-primary-500/30'
                  : 'bg-surface-subtle text-content-subtle cursor-not-allowed'
              ]"
              aria-label="发送"
            >
              <Send class="w-4 h-4" />
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 聊天输入区组件
 * @description 卡片式输入框（无边框输入 + 底部工具条），工具条含联网搜索/深度思考开关、
 * 问题优化、字数与发送按钮。输入框上方按需显示问题分类、知识库推荐、智能推荐与重写结果，
 * 均为单行轻量样式，减少布局跳动。快捷问题已移至消息列表空状态 Welcome。
 *
 * @props modelValue - 输入框文本（v-model）
 * @props rewriteResult - 问题重写结果，非空时显示重写结果横幅
 * @props questionClassification - 问题分类结果（type/subtype/confidence/description）
 * @props kbRecommendations - 智能推荐的知识库列表
 * @props selectedKbs - 当前已选中的知识库 ID 列表
 * @props suggestions - 智能推荐问题列表
 * @props isGeneratingSuggestions - 是否正在生成推荐问题
 * @props isRewritingQuestion - 是否正在重写问题
 * @props isTyping - 是否正在等待回答（用于禁用发送按钮）
 * @props getKbName - 由父组件提供的知识库 ID → 名称映射函数
 * @props useWebSearch - 联网搜索开关状态
 * @props searchStatus - 联网搜索执行状态（用于按钮文案）
 * @props deepThinking - 深度思考开关（开=模型先推理再回答，关=直接生成）
 * @props deepThinkingLabel - 深度思考按钮文案
 * @props deepThinkingTitle - 深度思考按钮悬停提示
 *
 * @emits update:modelValue - 输入文本变化
 * @emits send - 发送当前问题
 * @emits stop - 停止当前生成（流式回答进行中时替换发送按钮）
 * @emits send-quick-question - 发送推荐问题
 * @emits rewrite-question - 触发问题重写
 * @emits use-rewritten-question - 采用重写后的问题
 * @emits clear-rewrite - 关闭重写结果横幅
 * @emits input-change - 输入框内容变化（用于触发推荐/清空重写）
 * @emits toggle-kb-id - 切换知识库选中态
 * @emits toggle-web-search - 切换联网搜索开关
 * @emits toggle-deep-thinking - 切换深度思考开关
 */
import { computed, ref } from 'vue'
import { Sparkles, X, Lightbulb, Database, Loader2, Send, Globe, Brain, Square } from '@lucide/vue'
import type { RewriteResponse } from '@/queries/chat'
import type { KBRecommendation } from '@/queries/kb'

const props = defineProps<{
  modelValue: string
  rewriteResult: RewriteResponse | null
  questionClassification: { type: string; subtype: string; confidence: number; description: string } | null
  kbRecommendations: KBRecommendation[]
  selectedKbs: string[]
  suggestions: string[]
  isGeneratingSuggestions: boolean
  isRewritingQuestion: boolean
  isTyping: boolean
  getKbName: (kbId: string) => string
  useWebSearch: boolean
  searchStatus: 'idle' | 'searching' | 'done' | 'failed'
  deepThinking: boolean
  deepThinkingLabel: string
  deepThinkingTitle: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'send'): void
  (e: 'stop'): void
  (e: 'send-quick-question', question: string): void
  (e: 'rewrite-question'): void
  (e: 'use-rewritten-question'): void
  (e: 'clear-rewrite'): void
  (e: 'input-change'): void
  (e: 'toggle-kb-id', kbId: string): void
  (e: 'toggle-web-search'): void
  (e: 'toggle-deep-thinking'): void
}>()

const maxLength = 2000
const textareaRef = ref<HTMLTextAreaElement | null>(null)

// 输入框内容双向同步到父组件（父组件的 SSE/重写/对比逻辑需读取该值）
const questionInput = computed<string>({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
})

/** 联网搜索按钮文案：执行状态优先于开关状态 */
const webSearchLabel = computed(() => {
  if (props.searchStatus === 'searching') return '搜索中'
  if (props.searchStatus === 'failed') return '搜索失败'
  if (props.searchStatus === 'done') return '搜索完成'
  return '联网搜索'
})

/** 联网搜索状态点样式 */
const webSearchDotClass = computed(() => {
  if (props.searchStatus === 'searching') return 'bg-yellow-400 animate-pulse'
  if (props.searchStatus === 'failed') return 'bg-red-400'
  if (props.searchStatus === 'done') return 'bg-green-400'
  return props.useWebSearch ? 'bg-primary-500' : 'bg-gray-300 dark:bg-gray-600'
})

/** 将问题分类枚举值映射为中文标签，未知类型默认显示为"探索性"。 */
function getQuestionTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    'FACTUAL': '事实性',
    'OPINION': '观点性',
    'OPERATIONAL': '操作性',
    'EXPLANATORY': '解释性',
    'COMPARATIVE': '比较性',
    'EXPLORATORY': '探索性'
  }
  return labels[type] || '探索性'
}

/** 输入框内容变化时自适应高度，并通知父组件触发推荐/清除重写结果。 */
function onInput(): void {
  if (textareaRef.value) {
    textareaRef.value.style.height = 'auto'
    textareaRef.value.style.height = Math.min(textareaRef.value.scrollHeight, 150) + 'px'
  }
  emit('input-change')
}

/** 点击发送按钮或按下 Enter 时触发，将发送事件委托给父组件处理。 */
function onSend(): void {
  emit('send')
}
</script>
