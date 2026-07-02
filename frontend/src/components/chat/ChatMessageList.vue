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
      <!-- 用户消息：单气泡结构 -->
      <div
        v-if="message.role === 'user'"
        class="max-w-3xl px-4 py-3 rounded-2xl chat-bubble-user text-gray-800 dark:text-gray-100 rounded-br-md"
      >
        <MarkdownRenderer :content="message.content" />
      </div>

      <!-- 助手消息：推理面板 + 气泡 + 气泡外来源卡片 + 气泡外反馈区，四者垂直排列 -->
      <div v-else class="flex flex-col max-w-3xl gap-1.5">
        <!-- 推理/搜索过程折叠面板（豆包/DeepSeek 风格，位于答案气泡上方） -->
        <ReasoningPanel :steps="message.reasoning" />

        <!-- 助手气泡：头像 + 正文 / 加载态 -->
        <div class="px-4 py-3 rounded-2xl chat-bubble-assistant text-white rounded-bl-md">
          <div class="flex items-center gap-2 mb-2">
            <Bot class="w-4 h-4 opacity-80" />
            <span class="text-xs opacity-80">智能助手</span>
          </div>

          <template v-if="message.isLoading">
            <span class="text-sm">{{ message.reasoning?.length ? '正在搜索...' : '正在思考...' }}</span>
            <div class="flex gap-1 inline-flex ml-2">
              <span class="w-2 h-2 bg-white rounded-full typing-indicator" style="animation-delay: 0ms"></span>
              <span class="w-2 h-2 bg-white rounded-full typing-indicator" style="animation-delay: 150ms"></span>
              <span class="w-2 h-2 bg-white rounded-full typing-indicator" style="animation-delay: 300ms"></span>
            </div>
          </template>
          <template v-else>
            <MarkdownRenderer :content="message.content" :sources="message.sources" @cite-click="onCiteClick(message, $event)" />
          </template>
        </div>

        <!-- 来源卡片（气泡外，浅色卡片，参考主流模型布局） -->
        <MessageSources
          v-if="!message.isLoading && message.sources?.length"
          :sources="message.sources"
          @navigate="onNavigateSource"
        />

        <!-- 反馈区（气泡外，来源下方） -->
        <div v-if="!message.isLoading" class="flex items-center gap-4 px-1">
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
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 消息列表组件
 * @description 渲染当前会话的对话消息，包括用户/助手气泡、加载态、推理/搜索折叠面板、参考来源、星级反馈评分等。
 *
 * @props messages - 当前会话的消息数组（user/assistant/isLoading/sources/feedback 等）
 *
 * @emits navigate-source - 点击参考来源时触发，携带来源元数据
 * @emits submit-feedback - 提交星级评分时触发，携带 messageId 与 rating
 *
 * @expose scrollToBottom - 仅在接近底部时自动滚动到最新消息，避免打断用户回看历史
 */
import { ref, nextTick } from 'vue'
import { Bot, Star } from '@lucide/vue'
import type { Message, MessageSource } from '@/queries/chat'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import MessageSources from '@/components/chat/MessageSources.vue'
import ReasoningPanel from '@/components/chat/ReasoningPanel.vue'

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

/** 点击正文内联引用上标时，按 index 定位对应来源并复用来源点击逻辑。 */
function onCiteClick(message: Message, sourceIndex: number): void {
  const source = message.sources?.find(s => s.index === sourceIndex)
  if (source) {
    emit('navigate-source', source)
  }
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
