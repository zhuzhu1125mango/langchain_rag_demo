<template>
  <div
    ref="messagesContainer"
    class="flex-1 overflow-y-auto"
    role="log"
    aria-live="polite"
    aria-label="对话消息"
  >
    <div class="mx-auto flex min-h-full w-full max-w-3xl flex-col gap-6 px-4 py-8 sm:px-6">
    <!-- 空状态 Welcome：无消息时展示引导与示例问题 -->
    <div v-if="!messages.length" class="flex flex-1 flex-col items-center justify-center text-center pb-6">
      <div class="w-14 h-14 rounded-2xl bg-primary-500 flex items-center justify-center mb-5 ring-1 ring-black/5 dark:ring-white/10">
        <Bot class="w-7 h-7 text-white" />
      </div>
      <h2 class="text-2xl sm:text-[28px] font-semibold tracking-tight text-content">今天想了解什么？</h2>
      <p class="text-sm text-content-muted mt-2 max-w-md leading-6">
        向知识库提问，或开启联网搜索获取实时信息
      </p>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 w-full max-w-2xl mt-8">
        <button
          v-for="question in displayQuestions"
          :key="question"
          @click="emit('send-quick-question', question)"
          class="group flex items-start justify-between gap-3 px-4 py-3.5 text-left bg-surface border border-line rounded-2xl transition-all hover:border-primary-300 dark:hover:border-primary-700 hover:shadow-hover"
        >
          <span class="text-sm leading-6 text-content-secondary group-hover:text-content line-clamp-2">{{ question }}</span>
          <ArrowUpRight class="w-4 h-4 mt-0.5 shrink-0 text-content-subtle group-hover:text-primary-500 transition-colors" />
        </button>
      </div>
      <div class="mt-6 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-xs text-content-subtle">
        <span class="inline-flex items-center gap-1.5"><BookOpen class="w-3.5 h-3.5" />知识库检索</span>
        <span class="inline-flex items-center gap-1.5"><Globe2 class="w-3.5 h-3.5" />联网搜索</span>
        <span class="inline-flex items-center gap-1.5"><BrainCircuit class="w-3.5 h-3.5" />深度思考</span>
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
      <!-- 用户消息：中性气泡，突出内容本身而非品牌色块 -->
      <div
        v-if="message.role === 'user'"
        class="max-w-[85%] sm:max-w-[75%] px-4 py-2.5 rounded-2xl rounded-br-md bg-chat-user text-content text-[15px] leading-7"
      >
        <MarkdownRenderer :content="message.content" />
      </div>

      <!-- 助手消息：无卡片正文流 + 推理面板 + 来源 + 操作区 -->
      <div v-else class="group flex flex-col max-w-full gap-2 w-full">
        <!-- 推理/搜索过程折叠面板（豆包/DeepSeek 风格，位于正文上方） -->
        <ReasoningPanel :steps="message.reasoning" />

        <!-- 模型深度思考折叠面板（思考流式期间展开，正文开始后折叠） -->
        <ThinkingPanel :thinking="message.thinking" :loading="message.isLoading" />

        <div class="flex items-center gap-2">
          <span class="w-6 h-6 rounded-full bg-primary-500 flex items-center justify-center ring-1 ring-black/5 dark:ring-white/10">
            <Bot class="w-3.5 h-3.5 text-white" />
          </span>
          <span class="text-xs font-medium text-content-tertiary">智能助手</span>
        </div>

        <div class="chat-bubble-assistant text-[15px] leading-7 text-content">
          <template v-if="message.isLoading">
            <div class="flex items-center gap-2 text-sm text-content-muted">
              <span>{{ message.thinking ? '正在思考...' : (message.reasoning?.length ? '正在搜索...' : '正在思考...') }}</span>
              <span class="flex gap-1">
                <span class="w-1.5 h-1.5 bg-primary-400 rounded-full typing-indicator" style="animation-delay: 0ms"></span>
                <span class="w-1.5 h-1.5 bg-primary-400 rounded-full typing-indicator" style="animation-delay: 150ms"></span>
                <span class="w-1.5 h-1.5 bg-primary-400 rounded-full typing-indicator" style="animation-delay: 300ms"></span>
              </span>
            </div>
          </template>
          <!-- W6 #41：消息级错误专用卡片（不再把错误文案写进 Markdown 正文） -->
          <template v-else-if="message.error">
            <div class="flex items-start gap-2 p-3 rounded-xl bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800">
              <AlertTriangle class="w-4 h-4 flex-shrink-0 text-red-500 dark:text-red-400 mt-0.5" />
              <div class="min-w-0">
                <p class="text-sm font-medium text-red-700 dark:text-red-300">回答生成失败</p>
                <p class="text-xs text-red-600/80 dark:text-red-400/80 mt-0.5 break-words">{{ message.error }}</p>
              </div>
            </div>
            <!-- 失败前已生成的部分内容继续展示，避免刷新后丢失 -->
            <div v-if="message.content" class="mt-3">
              <MarkdownRenderer :content="message.content" :sources="message.sources" @cite-click="onCiteClick(message, $event)" />
            </div>
            <p v-if="message.stopped" class="mt-1.5 text-xs text-content-subtle">生成已中断</p>
          </template>
          <template v-else>
            <MarkdownRenderer :content="message.content" :sources="message.sources" @cite-click="onCiteClick(message, $event)" />
            <p v-if="message.stopped" class="mt-1.5 text-xs text-content-subtle">已停止生成</p>
          </template>
        </div>

        <!-- AM-2 HITL：高危工具审批卡片（pending 时等待用户回执） -->
        <div
          v-if="message.approval?.status === 'pending'"
          class="flex items-start gap-2 p-3 rounded-xl border border-amber-300 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/20"
        >
          <AlertTriangle class="w-4 h-4 flex-shrink-0 text-amber-500 mt-0.5" />
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium text-amber-800 dark:text-amber-200">需要你确认后继续执行</p>
            <p class="text-xs text-amber-700/80 dark:text-amber-300/80 mt-0.5 break-all">
              工具 {{ message.approval.tool }}
              <span v-if="approvalArgsText(message)"> · {{ approvalArgsText(message) }}</span>
            </p>
            <div class="flex gap-2 mt-2">
              <button
                @click="emit('resolve-approval', message.id, true)"
                class="px-2.5 py-1 rounded-md text-xs bg-amber-500 text-white hover:bg-amber-600 transition-colors"
              >
                批准执行
              </button>
              <button
                @click="emit('resolve-approval', message.id, false)"
                class="px-2.5 py-1 rounded-md text-xs border border-amber-300 dark:border-amber-700 text-amber-700 dark:text-amber-300 hover:bg-amber-100 dark:hover:bg-amber-900/40 transition-colors"
              >
                拒绝
              </button>
            </div>
          </div>
        </div>
        <p
          v-else-if="message.approval"
          class="text-xs text-content-muted px-1"
        >
          工具 {{ message.approval.tool }}：{{ approvalStatusText(message) }}
        </p>

        <!-- 来源卡片（卡片外，浅色样式，参考主流模型布局） -->
        <MessageSources
          v-if="!message.isLoading && message.sources?.length"
          :sources="message.sources"
          @navigate="onNavigateSource"
        />

        <!-- 操作区（复制 / 重新生成 / 星级反馈） -->
        <div
          v-if="!message.isLoading"
          class="flex items-center justify-between px-1 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 hover:opacity-100 focus-within:opacity-100 transition-opacity"
        >
          <div class="flex items-center gap-1">
            <AppButton
              variant="ghost"
              size="icon"
              :title="copiedId === message.id ? '已复制' : '复制回答'"
              :aria-label="copiedId === message.id ? '已复制回答' : '复制回答'"
              @click="copyMessage(message)"
            >
              <Check v-if="copiedId === message.id" class="w-3.5 h-3.5 text-green-500" />
              <Copy v-else class="w-3.5 h-3.5" />
            </AppButton>
            <AppButton
              v-if="isLastAssistant(index)"
              variant="ghost"
              size="icon"
              title="重新生成"
              aria-label="重新生成回答"
              @click="emit('regenerate')"
            >
              <RefreshCw class="w-3.5 h-3.5" />
            </AppButton>
          </div>

          <div class="flex items-center gap-2">
            <div class="flex items-center gap-0.5" role="group" aria-label="回答评分">
              <button
                v-for="star in 5"
                :key="star"
                @click="onStarClick(message.id, star)"
                @mouseenter="setHoverRating(message.id, star)"
                @mouseleave="setHoverRating(message.id, undefined)"
                :disabled="!!message.feedback?.rating"
                class="p-0.5 transition-all disabled:cursor-default"
                :class="!message.feedback?.rating ? 'hover:scale-110 cursor-pointer' : ''"
                :aria-label="`评分 ${star} 星`"
                :aria-pressed="getEffectiveRating(message) >= star"
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
import { Bot, Star, Copy, Check, RefreshCw, AlertTriangle, ArrowUpRight, BookOpen, Globe2, BrainCircuit } from '@lucide/vue'
import type { Message, MessageSource } from '@/queries/chat'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import MessageSources from '@/components/chat/MessageSources.vue'
import ReasoningPanel from '@/components/chat/ReasoningPanel.vue'
import ThinkingPanel from '@/components/chat/ThinkingPanel.vue'
import { AppButton } from '@/components/ui'

const props = defineProps<{
  messages: Message[]
  quickQuestions?: string[]
}>()

const emit = defineEmits<{
  (e: 'navigate-source', source: MessageSource): void
  (e: 'submit-feedback', payload: { messageId: string; rating: number }): void
  (e: 'send-quick-question', question: string): void
  (e: 'regenerate'): void
  (e: 'resolve-approval', messageId: string, approved: boolean): void
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

/** 当前展示的评分值：已提交评分优先，其次为悬停预览。 */
function getEffectiveRating(message: Message): number {
  return message.feedback?.rating || hoverRatings.value[message.id] || 0
}

/** 计算单颗星星的样式类：已评分或悬停评级以内的高亮黄色，否则为灰色。 */
function getStarClass(message: Message, star: number): string {
  if (star <= getEffectiveRating(message)) {
    return 'text-yellow-400 fill-yellow-400'
  }
  return 'text-content-disabled'
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

/** AM-2：审批卡片中的参数预览（截断，避免撑破布局）。 */
function approvalArgsText(message: Message): string {
  const args = message.approval?.arguments
  if (!args) return ''
  try {
    const text = JSON.stringify(args)
    return text.length > 120 ? `${text.slice(0, 120)}…` : text
  } catch {
    return ''
  }
}

/** AM-2：审批结果文案（pending 由卡片渲染，此处覆盖其余状态）。 */
function approvalStatusText(message: Message): string {
  const status = message.approval?.status
  if (status === 'approved') return '已批准'
  if (status === 'rejected') return '已拒绝'
  if (status === 'expired') return '已过期'
  return ''
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
