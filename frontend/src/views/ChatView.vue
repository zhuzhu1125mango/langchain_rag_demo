<template>
  <div class="flex-1 flex h-full chat-canvas relative">
    <!-- 答案对比侧边面板 -->
    <CompareAnswerPanel
      :show="showComparePanel"
      :is-comparing="isComparing"
      :compare-result="compareResult"
      :get-kb-name="getKBName"
      @close="showComparePanel = false"
    />

    <div class="flex-1 flex flex-col h-full">
      <header class="relative z-20 h-16 shrink-0 glass-chrome border-b px-4 sm:px-6">
        <div class="h-full max-w-[1200px] mx-auto flex items-center justify-between gap-3">
          <div class="min-w-0">
            <h1 class="text-[15px] font-semibold tracking-tight text-content truncate">
              {{ chatStore.currentSession?.title || '智能助手' }}
            </h1>
            <p class="hidden sm:block text-xs text-content-muted mt-0.5">知识库问答 · 本地模型</p>
          </div>
          <div class="flex items-center gap-1 sm:gap-1.5 flex-shrink-0">
            <AppButton variant="ghost" size="nav" title="清空当前对话" @click="clearChat">
              <Trash2 class="w-4 h-4" />
              <span class="hidden md:inline">清空</span>
            </AppButton>
            <AppButton
              :variant="selectedKBs.length > 0 ? 'primary-soft' : 'outline'"
              size="nav"
              @click="showKBSelector = !showKBSelector"
            >
              <BookOpen class="w-4 h-4" />
              <span class="hidden sm:inline">{{ selectedKBs.length > 0 ? `${selectedKBs.length} 个知识库` : '选择知识库' }}</span>
            </AppButton>
            <AppButton
              variant="ghost"
              size="nav"
              :disabled="selectedKBs.length < 2 || !questionInput.trim()"
              title="对比多个知识库的答案"
              @click="handleCompareAnswers"
            >
              <GitCompare class="w-4 h-4" />
              <span class="hidden md:inline">对比答案</span>
            </AppButton>
          </div>
        </div>
      </header>

      <KnowledgeBaseSelector
        v-if="showKBSelector"
        v-model="selectedKBs"
        :knowledge-bases="knowledgeBases"
      />

      <ChatMessageList
        ref="messageListRef"
        :messages="chatStore.messages"
        :quick-questions="chatStore.quickQuestions"
        @navigate-source="navigateToSource"
        @submit-feedback="onSubmitFeedback"
        @send-quick-question="sendQuickQuestion"
        @regenerate="regenerateLast"
        @resolve-approval="resolveApproval"
      />

      <ChatInputArea
        v-model="questionInput"
        :rewrite-result="rewriteResult"
        :question-classification="questionClassification"
        :kb-recommendations="kbRecommendations"
        :selected-kbs="selectedKBs"
        :suggestions="suggestions"
        :is-generating-suggestions="isGeneratingSuggestions"
        :is-rewriting-question="isRewritingQuestion"
        :is-typing="chatStore.isTyping"
        :get-kb-name="getKBName"
        :use-web-search="useWebSearch"
        :search-status="searchStatus"
        :deep-thinking="deepThinking"
        :deep-thinking-label="deepThinkingLabel"
        :deep-thinking-title="deepThinkingTitle"
        :deep-thinking-disabled="deepThinkingDisabled"
        :available-models="availableModels"
        :selected-model-id="selectedModelId"
        :default-model-id="defaultModelId"
        @send="sendMessage()"
        @stop="stopGeneration"
        @send-quick-question="sendQuickQuestion"
        @rewrite-question="handleRewriteQuestion"
        @use-rewritten-question="useRewrittenQuestion"
        @clear-rewrite="rewriteResult = null"
        @input-change="onInputChange"
        @toggle-kb-id="toggleKBById"
        @toggle-web-search="useWebSearch = !useWebSearch"
        @toggle-deep-thinking="toggleDeepThinking"
        @update:selected-model-id="changeModel"
      />
    </div>

    <DocumentSourceModal
      :visible="showSourceModal"
      :doc-id="selectedSourceDocId"
      :chunk-index="selectedSourceChunkIndex"
      @close="closeSourceModal"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { useKnowledgeBases } from '@/queries/kb'
import { Trash2, BookOpen, GitCompare } from '@lucide/vue'
import DocumentSourceModal from '@/components/DocumentSourceModal.vue'
import { AppButton } from '@/components/ui'
import CompareAnswerPanel from '@/components/chat/CompareAnswerPanel.vue'
import KnowledgeBaseSelector from '@/components/chat/KnowledgeBaseSelector.vue'
import ChatMessageList from '@/components/chat/ChatMessageList.vue'
import ChatInputArea from '@/components/chat/ChatInputArea.vue'
import { useChatStream } from '@/composables/useChatStream'
import { useModelSelection } from '@/composables/useModelSelection'
import { useToast } from '@/composables/useToast'
import { useChatAssist } from '@/composables/useChatAssist'
import { useSessionLoader } from '@/composables/useSessionLoader'
import { useMessageInteractions } from '@/composables/useMessageInteractions'

/**
 * 聊天页面主视图（编排层）。
 *
 * 模板与会话/消息状态在此汇聚；具体逻辑已拆分至 composables：
 * - useChatStream：SSE 流式对话（发送/事件分发/在途流控制）
 * - useChatAssist：问题重写/分类、防抖推荐、答案对比
 * - useSessionLoader：会话历史装载与路由参数联动
 * - useMessageInteractions：星级反馈与来源导航
 */

const chatStore = useChatStore()
const toast = useToast()
const router = useRouter()
const questionInput = ref('')
const showKBSelector = ref(false)
const selectedKBs = ref<string[]>([])
const useWebSearch = ref(false)
const searchStatus = ref<'idle' | 'searching' | 'done' | 'failed'>('idle')
/** 深度思考开关：开=模型先推理再回答，关=直接生成回答（对齐主流产品交互） */
const deepThinking = ref(false)

// 用户可选模型：列表来自 /api/models/available，选择按用户存在 localStorage。
// 管理员的开放范围决定列表内容，前端只决定「界面上能不能点」——真校验在后端。
const {
  models: availableModels,
  defaultModelId,
  selectedModelId,
  thinkingControl,
  nextModelId
} = useModelSelection()

/** 档位后缀（与选择器文案一致）：toast 里也要说清「会思考」还是「直答」 */
function tierHint(control: string | null | undefined): string {
  if (control === 'always_on') return '（会思考）'
  if (control === 'off') return '（直答）'
  return ''
}

/**
 * 切换模型并给出反馈。
 *
 * 为什么必须有这条 toast：模型是这个产品里的「档位」，切完没有任何反馈，
 * 用户只能靠盯着选择器的小字确认；而流式回答进行中切换时，更容易误以为
 * 当前这条回答会跟着变（实际只影响下一条）。
 */
function changeModel(modelId: string): void {
  if (modelId === selectedModelId.value) return
  selectedModelId.value = modelId
  const name = availableModels.value.find(m => m.id === modelId)?.display_name
    ?? availableModels.value.find(m => m.is_default)?.display_name
    ?? '默认模型'
  toast.info(
    `已切换到 ${name}${modelId === '' ? '（默认）' : ''}${tierHint(thinkingControl.value)}`,
    chatStore.isTyping ? '当前回答不受影响，下一条消息生效' : undefined
  )
}

/**
 * 快捷键 Alt+M：按「默认 → 各模型 → 默认」循环切换。
 *
 * 加 `preventDefault` 是因为 macOS 上 Option+M 会输入字符 µ；
 * 中文输入法组合中（isComposing）不拦截，否则会和候选词选择打架。
 */
function onModelShortcut(e: KeyboardEvent): void {
  if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || e.isComposing) return
  if (e.key.toLowerCase() !== 'm') return
  e.preventDefault()
  const next = nextModelId()
  if (next === selectedModelId.value) return
  changeModel(next)
}

onMounted(() => window.addEventListener('keydown', onModelShortcut))
onBeforeUnmount(() => window.removeEventListener('keydown', onModelShortcut))

/**
 * 只有**思考可关**的模型才允许动这个开关。
 *
 * 三种情况都要灰，但说法不同：不会思考（off）、会思考但关不掉（always_on）。
 * 后者是最容易被忽略的：LM Studio 上的 qwen3 实测发什么参数都照样思考，
 * 开关亮着只会让用户以为「关掉了」，而实际一个字都没省。
 * 未知（null）不禁用——目录还没回来就灰掉会闪一下，且后端 `should_think`
 * 对 off / always_on 都不会发思考参数，判断不出时也不会造成错误行为。
 */
const deepThinkingDisabled = computed(() => {
  const control = thinkingControl.value
  return control !== null && control !== 'switchable'
})

// 不可控时这里不再是一个「开关」，而是一个说明：档位由模型决定。
// 文案必须指向下一步动作（去选择器换模型），否则灰掉的按钮只会让人困惑。
const deepThinkingLabel = computed(() => {
  if (thinkingControl.value === 'always_on') return '深度思考·始终开'
  if (deepThinkingDisabled.value) return '深度思考·不支持'
  return deepThinking.value ? '深度思考·开' : '深度思考·关'
})
const deepThinkingTitle = computed(() => {
  if (thinkingControl.value === 'always_on') {
    return '该模型总会先推理，且接口无法关闭；想要直答请选择标注「直答」的模型'
  }
  if (deepThinkingDisabled.value) {
    return '当前模型直接回答；想让它先推理请选择标注「会思考」的模型'
  }
  return deepThinking.value
    ? '已开启深度思考：模型先推理再回答，适合复杂问题'
    : '开启深度思考：模型先推理再回答，适合复杂问题'
})

// 切到不可控的模型时把开关复位：留着一个「开着但点不亮」的状态，
// 换回可控模型时会莫名其妙地又变成开启。
watch(deepThinkingDisabled, disabled => {
  if (disabled) deepThinking.value = false
})

function toggleDeepThinking(): void {
  if (deepThinkingDisabled.value) return
  deepThinking.value = !deepThinking.value
}

// 子组件实例引用：用于调用子组件暴露的方法
const messageListRef = ref<InstanceType<typeof ChatMessageList> | null>(null)

const { data: knowledgeBases } = useKnowledgeBases()

// 输入辅助：问题重写/分类、防抖推荐、答案对比
const {
  rewriteResult,
  isRewritingQuestion,
  questionClassification,
  handleRewriteQuestion,
  useRewrittenQuestion,
  suggestions,
  isGeneratingSuggestions,
  kbRecommendations,
  onInputChange,
  showComparePanel,
  isComparing,
  compareResult,
  handleCompareAnswers
} = useChatAssist({ questionInput, selectedKBs })

// SSE 流式对话（卸载时自动 abort 在途流）
const { sendMessage, sendQuickQuestion, regenerateLast, resolveApproval, stopGeneration } = useChatStream({
  questionInput,
  selectedKBs,
  useWebSearch,
  deepThinking,
  selectedModelId,
  searchStatus,
  suggestions,
  messageListRef
})

// 会话历史装载（挂载时按路由参数加载，监听参数变化）
useSessionLoader()

// 消息列表交互：星级反馈与来源导航
const {
  onSubmitFeedback,
  navigateToSource,
  showSourceModal,
  selectedSourceDocId,
  selectedSourceChunkIndex,
  closeSourceModal
} = useMessageInteractions()

/** 切换指定知识库 ID 的选中状态（供输入区推荐按钮调用）。 */
function toggleKBById(kbId: string): void {
  const index = selectedKBs.value.indexOf(kbId)
  if (index === -1) {
    selectedKBs.value.push(kbId)
  } else {
    selectedKBs.value.splice(index, 1)
  }
}

/** 根据 ID 获取知识库显示名称。 */
function getKBName(kbId: string): string {
  const kb = knowledgeBases.value?.find(kb => kb.id === kbId)
  return kb?.name || kbId
}

/** 清空当前对话并重置路由。 */
function clearChat(): void {
  chatStore.clearMessages()
  chatStore.setCurrentSession(null)
  router.replace({ path: '/', query: {} })
}
</script>
