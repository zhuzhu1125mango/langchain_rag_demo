<template>
  <div ref="messagesContainer" class="flex-1 overflow-y-auto px-6 py-4 space-y-4">
    <!-- 空状态 Welcome：无消息时展示引导与示例问题 -->
    <div v-if="!messages.length" class="h-full min-h-full flex flex-col items-center justify-center text-center -mt-8">
      <div class="w-14 h-14 rounded-2xl bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center mb-4 shadow-lg shadow-primary-500/20">
        <Bot class="w-7 h-7 text-white" />
      </div>
      <h2 class="text-lg font-semibold text-gray-800 dark:text-white">有什么可以帮您？</h2>
      <p class="text-sm text-gray-500 dark:text-gray-400 mt-1.5 mb-6">基于知识库的智能问答，点击下方问题开始对话</p>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 w-full max-w-xl">
        <button
          v-for="question in displayQuestions"
          :key="question"
          @click="emit('send-quick-question', question)"
          class="px-4 py-3 text-sm text-left text-gray-600 dark:text-gray-300 bg-white dark:bg-dark-800 border border-gray-200 dark:border-dark-600 rounded-xl hover:border-primary-400 dark:hover:border-primary-500 hover:shadow-sm transition-all truncate"
        >
          {{ question }}
        </button>
      </div>
    </div>

    <div
      v-for="(message, index) in messages"
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

      <!-- 助手消息：推理面板 + 白底卡片 + 气泡外来源卡片 + 操作区，垂直排列 -->
      <div v-else class="group flex flex-col max-w-3xl gap-1.5 w-full">
        <!-- 推理/搜索过程折叠面板（豆包/DeepSeek 风格，位于答案卡片上方） -->
        <ReasoningPanel :steps="message.reasoning" />

        <!-- 模型深度思考折叠面板（思考流式期间展开，正文开始后折叠） -->
        <ThinkingPanel :thinking="message.thinking" :loading="message.isLoading" />

        <!-- 助手卡片：身份标识 / 正文 / 加载态 -->
        <div class="px-4 py-3 rounded-2xl rounded-bl-md chat-bubble-assistant text-gray-800 dark:text-gray-100">
          <div class="flex items-center gap-2 mb-2">
            <span class="w-5 h-5 rounded-md bg-gradient-to-br from-primary-500 to-primary-600 flex items-center justify-center">
              <Bot class="w-3 h-3 text-white" />
            </span>
            <span class="text-xs font-medium text-gray-500 dark:text-gray-400">智能助手</span>
          </div>

          <template v-if="message.isLoading">
            <span class="text-sm text-gray-500 dark:text-gray-400">{{ message.thinking ? '正在思考...' : (message.reasoning?.length ? '正在搜索...' : '正在思考...') }}</span>
            <div class="flex gap-1 inline-flex ml-2">
              <span class="w-2 h-2 bg-primary-400 rounded-full typing-indicator" style="animation-delay: 0ms"></span>
              <span class="w-2 h-2 bg-primary-400 rounded-full typing-indicator" style="animation-delay: 150ms"></span>
              <span class="w-2 h-2 bg-primary-400 rounded-full typing-indicator" style="animation-delay: 300ms"></span>
            </div>
          </template>
          <template v-else>
            <MarkdownRenderer :content="message.content" :sources="message.sources" @cite-click="onCiteClick(message, $event)" />
          </template>
        </div>

        <!-- 来源卡片（卡片外，浅色样式，参考主流模型布局） -->
        <MessageSources
          v-if="!message.isLoading && message.sources?.length"
          :sources="message.sources"
          @navigate="onNavigateSource"
        />

        <!-- 操作区（复制 / 重新生成 / 星级反馈） -->
        <div
          v-if="!message.isLoading"
          class="flex items-center justify-between px-1 opacity-0 group-hover:opacity-100 hover:opacity-100 focus-within:opacity-100 transition-opacity"
        >
          <div class="flex items-center gap-1">
            <button
              @click="copyMessage(message)"
              class="p-1.5 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 transition-colors"
              :title="copiedId === message.id ? '已复制' : '复制回答'"
            >
              <Check v-if="copiedId === message.id" class="w-3.5 h-3.5 text-green-500" />
              <Copy v-else class="w-3.5 h-3.5" />
            </button>
            <button
              v-if="isLastAssistant(index)"
              @click="emit('regenerate')"
              class="p-1.5 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 transition-colors"
              title="重新生成"
            >
              <RefreshCw class="w-3.5 h-3.5" />
            </button>
          </div>

          <div class="flex items-center gap-2">
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
 * @description 渲染当前会话的对话消息，包括空状态 Welcome、用户/助手消息、加载态、
 * 推理/搜索折叠面板、参考来源、消息操作（复制/重新生成）与星级反馈评分。
 *
 * @props messages - 当前会话的消息数组（user/assistant/isLoading/sources/feedback 等）
 * @props quickQuestions - 空状态展示的示例问题列表
 *
 * @emits navigate-source - 点击参考来源时触发，携带来源元数据
 * @emits submit-feedback - 提交星级评分时触发，携带 messageId 与 rating
 * @emits send-quick-question - 空状态点击示例问题时触发
 * @emits regenerate - 点击重新生成时触发，由父组件重发最后一条用户问题
 *
 * @expose scrollToBottom - 仅在接近底部时自动滚动到最新消息，避免打断用户回看历史
 */
import { ref, computed, nextTick } from 'vue'
import { Bot, Star, Copy, Check, RefreshCw } from '@lucide/vue'
import type { Message, MessageSource } from '@/queries/chat'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import MessageSources from '@/components/chat/MessageSources.vue'
import ReasoningPanel from '@/components/chat/ReasoningPanel.vue'
import ThinkingPanel from '@/components/chat/ThinkingPanel.vue'

const props = defineProps<{
  messages: Message[]
  quickQuestions?: string[]
}>()

const emit = defineEmits<{
  (e: 'navigate-source', source: MessageSource): void
  (e: 'submit-feedback', payload: { messageId: string; rating: number }): void
  (e: 'send-quick-question', question: string): void
  (e: 'regenerate'): void
}>()

const messagesContainer = ref<HTMLElement | null>(null)
// 鼠标悬停时的星级评分（消息列表内部 UI 状态）
const hoverRatings = ref<Record<string, number>>({})
// 最近一次复制成功的消息 ID，用于按钮的对勾反馈
const copiedId = ref<string | null>(null)

// 快捷问题为空（接口未就绪/失败）时的兜底示例
const fallbackQuestions = [
  '请介绍一下知识库里的内容',
  '帮我总结文档的核心要点',
  '根据文档解释一下关键概念',
  '文档里有哪些最佳实践？'
]

/** 空状态展示的示例问题：优先使用父组件传入的快捷问题，为空时回退内置示例 */
const displayQuestions = computed(() =>
  props.quickQuestions?.length ? props.quickQuestions.slice(0, 4) : fallbackQuestions
)

/** 复制消息正文到剪贴板，成功后短暂显示对勾反馈。 */
async function copyMessage(message: Message): Promise<void> {
  try {
    await navigator.clipboard.writeText(message.content)
    copiedId.value = message.id
    setTimeout(() => {
      if (copiedId.value === message.id) copiedId.value = null
    }, 1500)
  } catch {
    // 剪贴板不可用（权限/非安全上下文）时静默失败
  }
}

/** 判断当前位置是否为最后一条助手消息（仅其上展示"重新生成"）。 */
function isLastAssistant(index: number): boolean {
  for (let i = index + 1; i < props.messages.length; i++) {
    const later = props.messages[i]
    if (later?.role === 'assistant') return false
  }
  return true
}

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
