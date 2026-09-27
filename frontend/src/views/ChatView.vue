<template>
  <div class="flex-1 flex h-full bg-gray-50 dark:bg-dark-900 relative">
    <!-- 答案对比侧边面板 -->
    <CompareAnswerPanel
      :show="showComparePanel"
      :is-comparing="isComparing"
      :compare-result="compareResult"
      :get-kb-name="getKBName"
      @close="showComparePanel = false"
    />

    <div class="flex-1 flex flex-col h-full">
      <header class="bg-white dark:bg-dark-800 border-b border-gray-200 dark:border-dark-600 px-6 py-4">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="text-xl font-semibold text-gray-800 dark:text-white">智能助手</h1>
            <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">基于知识库的智能问答系统</p>
          </div>
          <div class="flex items-center gap-2">
            <button
              @click="clearChat"
              class="flex items-center gap-2 px-3 py-2 text-sm text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-dark-700 hover:text-gray-700 dark:hover:text-gray-200 rounded-lg transition-colors"
              title="清空当前对话"
            >
              <Trash2 class="w-4 h-4" />
              <span>清空</span>
            </button>
            <button
              @click="showKBSelector = !showKBSelector"
              class="flex items-center gap-2 px-3 py-2 text-sm text-primary-600 dark:text-primary-400 bg-primary-50 dark:bg-primary-900/30 hover:bg-primary-100 dark:hover:bg-primary-900/40 rounded-lg transition-colors"
            >
              <BookOpen class="w-4 h-4" />
              <span>{{ selectedKBs.length > 0 ? `${selectedKBs.length}个知识库` : '选择知识库' }}</span>
            </button>
            <button
              @click="handleCompareAnswers"
              :disabled="selectedKBs.length < 2 || !questionInput.trim()"
              :class="[
                'flex items-center gap-2 px-3 py-2 text-sm rounded-lg border transition-colors',
                selectedKBs.length >= 2 && questionInput.trim()
                  ? 'border-gray-300 dark:border-dark-500 text-gray-700 dark:text-gray-200 hover:border-primary-400 dark:hover:border-primary-600 hover:text-primary-600 dark:hover:text-primary-400'
                  : 'border-gray-200 dark:border-dark-700 text-gray-400 dark:text-gray-600 cursor-not-allowed'
              ]"
            >
              <GitCompare class="w-4 h-4" />
              <span>对比答案</span>
            </button>
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
        @send="sendMessage()"
        @send-quick-question="sendQuickQuestion"
        @rewrite-question="handleRewriteQuestion"
        @use-rewritten-question="useRewrittenQuestion"
        @clear-rewrite="rewriteResult = null"
        @input-change="onInputChange"
        @toggle-kb-id="toggleKBById"
        @toggle-web-search="useWebSearch = !useWebSearch"
        @toggle-deep-thinking="toggleDeepThinking"
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
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { useKnowledgeBases } from '@/queries/kb'
import { Trash2, BookOpen, GitCompare } from '@lucide/vue'
import DocumentSourceModal from '@/components/DocumentSourceModal.vue'
import CompareAnswerPanel from '@/components/chat/CompareAnswerPanel.vue'
import KnowledgeBaseSelector from '@/components/chat/KnowledgeBaseSelector.vue'
import ChatMessageList from '@/components/chat/ChatMessageList.vue'
import ChatInputArea from '@/components/chat/ChatInputArea.vue'
import { useChatStream } from '@/composables/useChatStream'
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
const router = useRouter()
const questionInput = ref('')
const showKBSelector = ref(false)
const selectedKBs = ref<string[]>([])
const useWebSearch = ref(false)
const searchStatus = ref<'idle' | 'searching' | 'done' | 'failed'>('idle')
/** 深度思考开关：开=模型先推理再回答，关=直接生成回答（对齐主流产品交互） */
const deepThinking = ref(false)
const deepThinkingLabel = computed(() => (deepThinking.value ? '深度思考·开' : '深度思考·关'))
const deepThinkingTitle = computed(() =>
  deepThinking.value
    ? '已开启深度思考：模型先推理再回答，适合复杂问题'
    : '开启深度思考：模型先推理再回答，适合复杂问题'
)
function toggleDeepThinking(): void {
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
const { sendMessage, sendQuickQuestion, regenerateLast } = useChatStream({
  questionInput,
  selectedKBs,
  useWebSearch,
  deepThinking,
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
