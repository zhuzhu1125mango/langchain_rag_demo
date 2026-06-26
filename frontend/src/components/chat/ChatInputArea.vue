<template>
  <div class="bg-white dark:bg-dark-800 border-t border-gray-200 dark:border-dark-600 px-6 py-4">
    <!-- 问题重写结果显示 -->
    <div v-if="rewriteResult" class="mb-3 p-3 bg-primary-50 dark:bg-primary-900/20 border border-primary-200 dark:border-primary-800 rounded-lg">
      <div class="flex items-start justify-between gap-2">
        <div class="flex-1">
          <div class="flex items-center gap-2 mb-2">
            <Sparkles class="w-4 h-4 text-primary-500" />
            <span class="text-sm font-medium text-primary-600 dark:text-primary-400">问题已优化</span>
          </div>
          <p class="text-sm text-gray-700 dark:text-gray-300 mb-1">{{ rewriteResult.rewritten }}</p>
          <p v-if="rewriteResult.changes && rewriteResult.changes !== '问题无需修改'" class="text-xs text-gray-500 dark:text-gray-400">
            改动说明：{{ rewriteResult.changes }}
          </p>
        </div>
        <div class="flex items-center gap-2">
          <button
            @click="emit('use-rewritten-question')"
            class="px-3 py-1.5 text-xs bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
          >
            使用优化后的问题
          </button>
          <button
            @click="emit('clear-rewrite')"
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <X class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>

    <!-- 问题分类标签 -->
    <div v-if="questionClassification && !rewriteResult" class="mb-3 flex items-center gap-2">
      <span class="text-xs text-gray-500 dark:text-gray-400">问题类型：</span>
      <span :class="[
        'px-2 py-1 text-xs rounded-full font-medium',
        questionClassification.type === 'FACTUAL' ? 'bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400' :
        questionClassification.type === 'OPINION' ? 'bg-purple-100 dark:bg-purple-900/30 text-purple-600 dark:text-purple-400' :
        questionClassification.type === 'OPERATIONAL' ? 'bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400' :
        questionClassification.type === 'EXPLANATORY' ? 'bg-orange-100 dark:bg-orange-900/30 text-orange-600 dark:text-orange-400' :
        questionClassification.type === 'COMPARATIVE' ? 'bg-pink-100 dark:bg-pink-900/30 text-pink-600 dark:text-pink-400' :
        'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300'
      ]">
        {{ getQuestionTypeLabel(questionClassification.type) }}
      </span>
      <span v-if="questionClassification.subtype" class="text-xs text-gray-400 dark:text-gray-500">
        ({{ questionClassification.subtype }})
      </span>
    </div>

    <!-- 知识库智能推荐 -->
    <div v-if="kbRecommendations.length > 0 && !rewriteResult" class="mb-3 p-3 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
      <div class="flex items-center gap-2 mb-2">
        <Lightbulb class="w-4 h-4 text-blue-500" />
        <span class="text-sm font-medium text-blue-600 dark:text-blue-400">智能推荐知识库</span>
      </div>
      <div class="flex flex-wrap gap-2">
        <button
          v-for="rec in kbRecommendations"
          :key="rec.kb_id"
          @click="emit('toggle-kb-id', rec.kb_id)"
          :class="[
            'px-3 py-1.5 text-sm rounded-full flex items-center gap-1.5',
            selectedKbs.includes(rec.kb_id)
              ? 'bg-blue-500 text-white'
              : 'bg-white dark:bg-dark-700 text-gray-600 dark:text-gray-300 hover:bg-blue-100 dark:hover:bg-blue-900/30 border border-blue-200 dark:border-blue-800'
          ]"
        >
          <Database class="w-3.5 h-3.5" />
          <span>{{ getKbName(rec.kb_id) }}</span>
          <span class="text-xs opacity-70">({{ rec.relevance_score.toFixed(2) }})</span>
        </button>
      </div>
    </div>

    <div class="flex flex-wrap gap-2 mb-3">
      <button
        v-for="question in quickQuestions"
        :key="question"
        @click="emit('send-quick-question', question)"
        class="px-3 py-1.5 text-sm bg-gray-100 dark:bg-dark-700 text-gray-600 dark:text-gray-300 rounded-full hover:bg-gray-200 dark:hover:bg-dark-600 transition-colors"
      >
        {{ question }}
      </button>
    </div>

    <div v-if="suggestions.length > 0 || isGeneratingSuggestions" class="mb-3">
      <div class="flex items-center gap-2 mb-2">
        <Sparkles class="w-4 h-4 text-primary-500" />
        <span class="text-xs text-gray-500 dark:text-gray-400">智能推荐</span>
        <span v-if="isGeneratingSuggestions" class="flex gap-1 inline-flex">
          <span class="w-1.5 h-1.5 bg-primary-500 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
          <span class="w-1.5 h-1.5 bg-primary-500 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
          <span class="w-1.5 h-1.5 bg-primary-500 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
        </span>
      </div>
      <div class="flex flex-wrap gap-2">
        <button
          v-for="(suggestion, index) in suggestions"
          :key="index"
          @click="emit('send-quick-question', suggestion)"
          class="px-3 py-1.5 text-sm bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400 rounded-full hover:bg-primary-100 dark:hover:bg-primary-900/40 transition-colors border border-primary-200 dark:border-primary-800"
        >
          {{ suggestion }}
        </button>
      </div>
    </div>

    <div class="flex items-end gap-3">
      <div class="flex-1 relative">
        <textarea
          ref="textareaRef"
          v-model="questionInput"
          @keydown.enter.exact.prevent="onSend"
          @input="onInput"
          placeholder="输入您的问题..."
          rows="1"
          class="w-full px-4 py-3 pr-20 bg-gray-100 dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 rounded-xl resize-none focus:outline-none focus:ring-2 focus:ring-primary-500 focus:bg-white dark:focus:bg-dark-600 transition-all"
          :maxlength="maxLength"
        ></textarea>
        <div class="absolute right-2 bottom-2 flex items-center gap-1">
          <button
            v-if="questionInput.trim() && !rewriteResult"
            @click="emit('rewrite-question')"
            :disabled="isRewritingQuestion"
            class="p-1.5 text-gray-400 hover:text-primary-500 transition-colors disabled:opacity-50"
            title="优化问题"
          >
            <Loader2 v-if="isRewritingQuestion" class="w-4 h-4 animate-spin" />
            <Sparkles v-else class="w-4 h-4" />
          </button>
          <span
            :class="[
              'text-xs',
              questionInput.length >= maxLength ? 'text-red-500' : 'text-gray-400'
            ]"
          >
            {{ questionInput.length }}/{{ maxLength }}
          </span>
        </div>
      </div>
      <button
        @click="onSend"
        :disabled="!questionInput.trim() || isTyping"
        :class="[
          'p-3 rounded-xl transition-all',
          questionInput.trim() && !isTyping
            ? 'bg-gradient-to-r from-primary-500 to-primary-600 text-white hover:from-primary-600 hover:to-primary-700'
            : 'bg-gray-200 dark:bg-dark-600 text-gray-400 cursor-not-allowed'
        ]"
      >
        <Send class="w-5 h-5" />
      </button>
    </div>
    <p class="text-xs text-gray-400 mt-2">按 Enter 发送，Ctrl + Enter 换行 | 输入问题后可点击魔棒图标优化问题表述</p>
  </div>
</template>

<script setup lang="ts">
/**
 * 聊天输入区组件
 * @description 包含问题输入框、问题分类提示、知识库智能推荐、问题重写结果、快捷问题与智能推荐等模块。
 * 父组件通过 v-model:modelValue 维护输入文本，通过各类 emit 触发发送、重写、推荐等行为。
 *
 * @props modelValue - 输入框文本（v-model）
 * @props rewriteResult - 问题重写结果，非空时显示重写结果横幅
 * @props questionClassification - 问题分类结果（type/subtype/confidence/description）
 * @props kbRecommendations - 智能推荐的知识库列表
 * @props selectedKbs - 当前已选中的知识库 ID 列表
 * @props suggestions - 智能推荐问题列表
 * @props isGeneratingSuggestions - 是否正在生成推荐问题
 * @props isRewritingQuestion - 是否正在重写问题
 * @props quickQuestions - 快捷问题列表
 * @props isTyping - 是否正在等待回答（用于禁用发送按钮）
 * @props getKbName - 由父组件提供的知识库 ID → 名称映射函数
 *
 * @emits update:modelValue - 输入文本变化
 * @emits send - 发送当前问题
 * @emits send-quick-question - 发送快捷/推荐问题
 * @emits rewrite-question - 触发问题重写
 * @emits use-rewritten-question - 采用重写后的问题
 * @emits clear-rewrite - 关闭重写结果横幅
 * @emits input-change - 输入框内容变化（用于触发推荐/清空重写）
 * @emits toggle-kb-id - 切换知识库选中态
 */
import { computed, ref } from 'vue'
import { Sparkles, X, Lightbulb, Database, Loader2, Send } from '@lucide/vue'
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
  quickQuestions: string[]
  isTyping: boolean
  getKbName: (kbId: string) => string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'send'): void
  (e: 'send-quick-question', question: string): void
  (e: 'rewrite-question'): void
  (e: 'use-rewritten-question'): void
  (e: 'clear-rewrite'): void
  (e: 'input-change'): void
  (e: 'toggle-kb-id', kbId: string): void
}>()

const maxLength = 2000
const textareaRef = ref<HTMLTextAreaElement | null>(null)

// 输入框内容双向同步到父组件（父组件的 SSE/重写/对比逻辑需读取该值）
const questionInput = computed<string>({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v)
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
