<template>
  <div ref="messagesContainer" class="flex-1 overflow-y-auto px-6 py-4 space-y-4">
    <div
      v-for="message in messages"
      :key="message.id"
      :class="[
        'flex fade-in',
        message.role === 'user' ? 'justify-end' : 'justify-start'
      ]"
    >
      <div
        :class="[
          'max-w-3xl px-4 py-3 rounded-2xl',
          message.role === 'user'
            ? 'chat-bubble-user text-gray-800 dark:text-gray-100 rounded-br-md'
            : 'chat-bubble-assistant text-white rounded-bl-md'
        ]"
      >
        <div v-if="message.role === 'assistant'" class="flex items-center gap-2 mb-2">
          <Bot class="w-4 h-4 opacity-80" />
          <span class="text-xs opacity-80">智能助手</span>
        </div>

        <template v-if="message.isLoading">
          <span class="text-sm">正在思考...</span>
          <div class="flex gap-1 inline-flex ml-2">
            <span class="w-2 h-2 bg-white rounded-full typing-indicator" style="animation-delay: 0ms"></span>
            <span class="w-2 h-2 bg-white rounded-full typing-indicator" style="animation-delay: 150ms"></span>
            <span class="w-2 h-2 bg-white rounded-full typing-indicator" style="animation-delay: 300ms"></span>
          </div>
        </template>
        <template v-else>
          <MarkdownRenderer :content="message.content" />
          <div v-if="message.sources?.length" class="mt-3 pt-3 border-t border-white/20">
            <p class="text-xs opacity-80 mb-2">参考来源：</p>
            <div class="space-y-1">
              <div
                v-for="(source, idx) in message.sources"
                :key="idx"
                class="flex items-start gap-2 text-xs opacity-60 hover:opacity-100 transition-opacity cursor-pointer group"
                @click="onNavigateSource(source)"
              >
                <ExternalLink class="w-3 h-3 mt-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity" />
                <div class="flex-1 min-w-0">
                  <p class="font-medium truncate">
                    {{ source.source_type === 'web' ? (source.title || source.document_name || '网页来源') : (source.document_name || '未知文档') }}
                  </p>
                  <p v-if="source.source_type === 'web' && source.url" class="text-[10px] opacity-70 truncate">
                    {{ source.url }}
                  </p>
                </div>
              </div>
            </div>
          </div>
          <div v-if="message.role === 'assistant'" class="mt-3 flex items-center gap-4">
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-500 dark:text-gray-400">
                {{ message.feedback?.rating ? '已评价' : '评价回答' }}
              </span>
              <div class="flex items-center gap-0.5">
                <button
                  v-for="star in 5"
                  :key="star"
                  @click="onStarClick(message.id, star)"
                  @mouseenter="setHoverRating(message.id, star)"
                  @mouseleave="setHoverRating(message.id, undefined)"
                  :disabled="!!message.feedback?.rating"
                  class="p-0.5 transition-all disabled:cursor-default"
                  :class="!message.feedback?.rating ? 'hover:scale-110 cursor-pointer' : ''"
                >
                  <Star
                    class="w-4 h-4 transition-colors"
                    :class="getStarClass(message, star)"
                  />
                </button>
              </div>
              <span v-if="message.feedback?.rating" class="text-xs text-primary-600 dark:text-primary-400">
                {{ message.feedback.rating }} 星
              </span>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 消息列表组件
 * @description 渲染当前会话的对话消息，包括用户/助手气泡、加载态、参考来源、星级反馈评分等。
 *
 * @props messages - 当前会话的消息数组（user/assistant/isLoading/sources/feedback 等）
 *
 * @emits navigate-source - 点击参考来源时触发，携带来源元数据
 * @emits submit-feedback - 提交星级评分时触发，携带 messageId 与 rating
 *
 * @expose scrollToBottom - 仅在接近底部时自动滚动到最新消息，避免打断用户回看历史
 */
import { ref, nextTick } from 'vue'
import { Bot, ExternalLink, Star } from '@lucide/vue'
import type { Message, MessageSource } from '@/queries/chat'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'

defineProps<{
  messages: Message[]
}>()

const emit = defineEmits<{
  (e: 'navigate-source', source: MessageSource): void
  (e: 'submit-feedback', payload: { messageId: string; rating: number }): void
}>()

const messagesContainer = ref<HTMLElement | null>(null)
// 鼠标悬停时的星级评分（消息列表内部 UI 状态）
const hoverRatings = ref<Record<string, number>>({})

/** 计算单颗星星的样式类：已评分或悬停评级以内的高亮黄色，否则为灰色。 */
function getStarClass(message: Message, star: number): string {
  const effectiveRating = message.feedback?.rating || hoverRatings.value[message.id] || 0
  if (star <= effectiveRating) {
    return 'text-yellow-400 fill-yellow-400'
  }
  return 'text-gray-300 dark:text-gray-600'
}

/** 设置/清除鼠标悬停时的临时评分（未提交前的预览高亮）。 */
function setHoverRating(messageId: string, rating?: number): void {
  if (rating === undefined) {
    delete hoverRatings.value[messageId]
  } else {
    hoverRatings.value[messageId] = rating
  }
}

/** 点击参考来源时向父组件冒泡 navigate-source 事件。 */
function onNavigateSource(source: MessageSource): void {
  emit('navigate-source', source)
}

/** 点击星星提交评分，向上冒泡 submit-feedback 事件。 */
function onStarClick(messageId: string, rating: number): void {
  emit('submit-feedback', { messageId, rating })
}

/** 判断滚动容器是否接近底部（阈值 50px），用于决定自动滚动是否打断用户阅读。 */
function isNearBottom(): boolean {
  if (!messagesContainer.value) return true
  const container = messagesContainer.value
  const scrollTop = container.scrollTop
  const scrollHeight = container.scrollHeight
  const clientHeight = container.clientHeight
  return scrollHeight - scrollTop - clientHeight < 50
}

/** 仅在接近底部时自动滚动到最新消息。 */
function scrollToBottom(): void {
  nextTick(() => {
    if (messagesContainer.value && isNearBottom()) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

defineExpose({ scrollToBottom })
</script>
