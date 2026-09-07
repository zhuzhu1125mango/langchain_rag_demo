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
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQueryClient } from '@tanstack/vue-query'
import { useChatStore } from '@/stores/chat'
import { useSubmitFeedback, useGetSuggestions, useRewriteQuestion, useClassifyQuestion, useCompareKnowledgeBases } from '@/queries/chat'
import type { Message, MessageSource, CompareResponse, ReasoningStep } from '@/queries/chat'
import { useKnowledgeBases, useRecommendKnowledgeBases } from '@/queries/kb'
import type { KBRecommendation } from '@/queries/kb'
import { Trash2, BookOpen, GitCompare } from '@lucide/vue'
import DocumentSourceModal from '@/components/DocumentSourceModal.vue'
import CompareAnswerPanel from '@/components/chat/CompareAnswerPanel.vue'
import KnowledgeBaseSelector from '@/components/chat/KnowledgeBaseSelector.vue'
import ChatMessageList from '@/components/chat/ChatMessageList.vue'
import ChatInputArea from '@/components/chat/ChatInputArea.vue'
import { useToast } from '@/composables/useToast'
import { api } from '@/utils/axios'
import { generateId } from '@/utils/id'
import { mergeReasoningSteps } from '@/utils/reasoning'
import { openExternalUrl } from '@/utils/url'

/**
 * 聊天页面主视图。
 *
 * 负责会话/消息状态编排、SSE 流式对话、问题重写/分类、知识库推荐与答案对比。
 * 会话列表、对比面板、知识库选择器、消息列表、输入区已拆分为独立子组件，
 * 通过 props/emits 与本视图交互。
 */

const chatStore = useChatStore()
const queryClient = useQueryClient()
const route = useRoute()
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
/** 切换深度思考开关 */
function toggleDeepThinking(): void {
  deepThinking.value = !deepThinking.value
}
const suggestions = ref<string[]>([])
const isGeneratingSuggestions = ref(false)
const rewriteResult = ref<{ rewritten: string; original: string; changes: string } | null>(null)
const isRewritingQuestion = ref(false)
const questionClassification = ref<{ type: string; subtype: string; confidence: number; description: string } | null>(null)
const showComparePanel = ref(false)
const isComparing = ref(false)
const compareResult = ref<CompareResponse | null>(null)
const kbRecommendations = ref<KBRecommendation[]>([])
const showSourceModal = ref(false)
const selectedSourceDocId = ref('')
const selectedSourceChunkIndex = ref(0)
// fetchSuggestions 与 fetchKBRecommendations 共用同一个防抖计时器：
// 输入变化时两个函数依次调用，后者会清掉前者刚注册的定时器，从而保证
// 一次输入抖动内只有最后一次注册的请求真正发出，避免并发重复请求。
let debounceTimer: ReturnType<typeof setTimeout> | null = null

// SSE reasoning 事件节流：同一帧内多次 reasoning 更新合并为一次状态提交，
// 避免高频时间线更新导致渲染抖动。
let pendingReasoningUpdate: ReasoningStep[] | null = null
let reasoningRafId: number | null = null

/** 后端来源元数据的原始结构（SSE end 事件与会话历史加载共用）。 */
interface RawSourceMeta {
  source?: string
  score?: number
  filename?: string
  url?: string
  title?: string
  source_type?: string
  content?: string
  page_content?: string
  document_id?: string
  chunk_index?: number
  total_chunks?: number
}

/** 取消挂起的 reasoning 节流更新。 */
function cancelReasoningRaf(): void {
  if (reasoningRafId !== null) {
    cancelAnimationFrame(reasoningRafId)
    reasoningRafId = null
  }
  pendingReasoningUpdate = null
}

// 子组件实例引用：用于调用子组件暴露的方法
const messageListRef = ref<InstanceType<typeof ChatMessageList> | null>(null)

const feedbackMutation = useSubmitFeedback()
const getSuggestions = useGetSuggestions()
const rewriteQuestionMutation = useRewriteQuestion()
const classifyQuestionMutation = useClassifyQuestion()
const compareKBsMutation = useCompareKnowledgeBases()
const recommendKBsMutation = useRecommendKnowledgeBases()

const { data: knowledgeBases } = useKnowledgeBases()


const toast = useToast()

/** 切换指定知识库 ID 的选中状态（供输入区推荐按钮调用）。 */
function toggleKBById(kbId: string): void {
  const index = selectedKBs.value.indexOf(kbId)
  if (index === -1) {
    selectedKBs.value.push(kbId)
  } else {
    selectedKBs.value.splice(index, 1)
  }
}

/** 重写问题并自动分类，失败时回退到原始问题分类。 */
async function handleRewriteQuestion(): Promise<void> {
  if (!questionInput.value.trim() || isRewritingQuestion.value) return

  isRewritingQuestion.value = true
  questionClassification.value = null

  try {
    const result = await rewriteQuestionMutation.mutateAsync(questionInput.value)
    rewriteResult.value = result

    // 自动进行问题分类
    await handleClassifyQuestion(result.rewritten || result.original || questionInput.value)
  } catch (error) {
    console.error('问题重写失败:', error)
    toast.error('问题重写失败', '请稍后重试')
    // 失败时仍然进行分类
    await handleClassifyQuestion(questionInput.value)
  } finally {
    isRewritingQuestion.value = false
  }
}

/** 调用后端对问题进行类型分类。 */
async function handleClassifyQuestion(question: string): Promise<void> {
  try {
    const result = await classifyQuestionMutation.mutateAsync(question)
    questionClassification.value = result
  } catch (error) {
    console.error('问题分类失败:', error)
    questionClassification.value = null
  }
}

/** 将输入框替换为重写后的问题。 */
function useRewrittenQuestion(): void {
  if (rewriteResult.value) {
    questionInput.value = rewriteResult.value.rewritten
    rewriteResult.value = null
  }
}

/** 对选中的多个知识库进行答案对比。 */
async function handleCompareAnswers(): Promise<void> {
  if (selectedKBs.value.length < 2 || !questionInput.value.trim()) return

  showComparePanel.value = true
  isComparing.value = true
  compareResult.value = null

  try {
    const result = await compareKBsMutation.mutateAsync({
      question: questionInput.value,
      kbIds: selectedKBs.value
    })
    compareResult.value = result
  } catch (error) {
    console.error('答案对比失败:', error)
    toast.error('答案对比失败', '请稍后重试')
  } finally {
    isComparing.value = false
  }
}

/** 根据 ID 获取知识库显示名称。 */
function getKBName(kbId: string): string {
  const kb = knowledgeBases.value?.find(kb => kb.id === kbId)
  return kb?.name || kbId
}

/** 点击来源时跳转网页或打开文档切片弹窗。 */
function navigateToSource(source: MessageSource): void {
  if (source.source_type === 'web' && source.url) {
    openExternalUrl(source.url)
    return
  }
  // kb 来源优先用 document_id 定位文档切片，回退到 document_name
  const docId = source.document_id || source.document_name
  if (docId) {
    selectedSourceDocId.value = docId
    selectedSourceChunkIndex.value = source.chunk_index ?? source.page ?? 0
    showSourceModal.value = true
  }
}

function closeSourceModal(): void {
  showSourceModal.value = false
}

/** 输入框内容变化时清除重写结果并触发推荐。 */
function onInputChange(): void {
  if (rewriteResult.value) {
    rewriteResult.value = null
  }

  fetchSuggestions()
  fetchKBRecommendations()
}

/** 防抖获取与当前问题相关的知识库推荐。 */
async function fetchKBRecommendations(): Promise<void> {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
  }

  const question = questionInput.value.trim()

  if (question.length < 5) {
    kbRecommendations.value = []
    return
  }

  debounceTimer = setTimeout(async () => {
    try {
      const result = await recommendKBsMutation.mutateAsync({ question, top_k: 3 })
      kbRecommendations.value = result
    } catch (error) {
      console.error('获取知识库推荐失败:', error)
      kbRecommendations.value = []
    }
  }, 800)
}

/** 防抖获取基于当前输入的智能问题推荐。 */
async function fetchSuggestions(): Promise<void> {
  if (debounceTimer) {
    clearTimeout(debounceTimer)
  }

  const question = questionInput.value.trim()

  if (question.length < 3) {
    suggestions.value = []
    return
  }

  debounceTimer = setTimeout(async () => {
    isGeneratingSuggestions.value = true

    try {
      const result = await getSuggestions(question, undefined, selectedKBs.value.length > 0 ? selectedKBs.value : undefined)
      suggestions.value = result
    } catch (error) {
      console.error('获取推荐问题失败:', error)
    } finally {
      isGeneratingSuggestions.value = false
    }
  }, 500)
}

/** 发送用户问题，通过 SSE 接收流式回答；传入 overrideQuestion 用于重新生成场景。 */
async function sendMessage(overrideQuestion?: string): Promise<void> {
  const question = (overrideQuestion ?? questionInput.value).trim()
  if (!question) return

  const hasLoadingMessage = chatStore.messages.some(m => m.role === 'assistant' && m.isLoading)
  if (hasLoadingMessage) return

  if (useWebSearch.value) {
    searchStatus.value = 'searching'
  }

  questionInput.value = ''

  chatStore.addMessage({
    id: generateId(),
    role: 'user',
    content: question
  })

  chatStore.addQuestionToHistory(question)

  suggestions.value = []

  messageListRef.value?.scrollToBottom()

  try {
    const assistantMsgId = generateId()
    chatStore.addMessage({
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      sources: [],
      isLoading: true
    })

    messageListRef.value?.scrollToBottom()

    // 改用 POST + fetch 流式读取（替代 EventSource）：
    //   - question 不再走 URL query，避免进入 nginx 访问日志与浏览器历史
    //   - EventSource 无法携带自定义头，POST 可统一走 X-API-Key 认证
    const apiKey = localStorage.getItem('api_key') || import.meta.env.VITE_API_KEY
    const requestHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
    if (apiKey) {
      requestHeaders['X-API-Key'] = apiKey
    }
    const savedToken = localStorage.getItem('token')
    if (savedToken) {
      requestHeaders['Authorization'] = `Bearer ${savedToken}`
    }

    const requestBody: Record<string, unknown> = {
      question,
      use_web_search: useWebSearch.value,
      deep_thinking: deepThinking.value ? 'on' : 'off'
    }

    if (useWebSearch.value) {
      requestBody.search_mode = 'function_calling'
    }

    if (selectedKBs.value.length > 0) {
      requestBody.kb_ids = selectedKBs.value
    }

    if (chatStore.currentSession?.id) {
      requestBody.session_id = chatStore.currentSession.id
    }

    const controller = new AbortController()
    let receivedContent = false
    let receivedEnd = false
    let streamEnded = false
    const closeStream = () => {
      if (!streamEnded) {
        streamEnded = true
        controller.abort()
      }
    }

    // SSE 事件协议：
    //   - content：data.content 为增量文本片段，累加到助手消息内容上
    //   - thinking：模型原始思考增量（深度思考开启时），累加到助手消息 thinking 上
    //   - reasoning：搜索/思考过程结构化数据，供前端折叠面板展示
    //   - search_status：向后兼容，映射为 reasoning 步骤并更新搜索按钮状态
    //   - end：回答结束，data.sources 为来源列表，data.message_id 为后端正式消息 ID，
    //          data.session_id 用于新会话创建，data.reasoning 为完整推理过程；
    //          收到 end 后不主动断开，等待后端补发 title 事件后由服务端关闭连接
    //   - title：后台生成的会话标题（C7：end 先发、标题后补），更新侧栏会话标题
    //   - error：data.error 为错误描述，展示后关闭连接
    const processEvent = (eventData: string) => {
      try {
        const data = JSON.parse(eventData)
        const lastMsg = chatStore.messages[chatStore.messages.length - 1]
        if (!lastMsg) return

        if (data.type === 'search_status') {
          searchStatus.value = data.status || 'idle'
          return
        }

        if (data.type === 'thinking') {
          if (data.content) {
            const updatedThinking = (lastMsg.thinking || '') + data.content
            chatStore.updateMessage(lastMsg.id, { thinking: updatedThinking })
            messageListRef.value?.scrollToBottom()
          }
          return
        }

        if (data.type === 'reasoning') {
          const step = data as ReasoningStep
          const current = lastMsg.reasoning || []
          const merged = mergeReasoningSteps(current, [step])
          chatStore.updateMessage(lastMsg.id, { reasoning: merged })
          return
        }

        if (data.type === 'title') {
          // C7：end 先发，标题后台生成完成后补发；更新当前会话标题并刷新侧栏列表
          if (data.title && chatStore.currentSession) {
            chatStore.updateCurrentSessionTitle(data.title)
          }
          queryClient.invalidateQueries({ queryKey: ['sessions'] })
          queryClient.refetchQueries({ queryKey: ['sessions'] })
          return
        }

        if (data.type === 'content') {
          if (data.content) {
            receivedContent = true
            const updatedContent = (lastMsg.content || '') + data.content
            chatStore.updateMessage(lastMsg.id, { content: updatedContent, isLoading: false })
            messageListRef.value?.scrollToBottom()
          }
        } else if (data.type === 'end') {
          receivedEnd = true
          // 强制 flush 可能挂起的 reasoning 更新
          cancelReasoningRaf()
          if (data.reasoning && Array.isArray(data.reasoning)) {
            const finalReasoning = data.reasoning as ReasoningStep[]
            const current = lastMsg.reasoning || []
            chatStore.updateMessage(lastMsg.id, { reasoning: mergeReasoningSteps(current, finalReasoning) })
          }
          if (data.sources) {
            const mappedSources: MessageSource[] = data.sources.map((s: RawSourceMeta, idx: number) => ({
              source: s.source || s.document_id || s.filename || '',
              score: s.score || 0,
              document_name: s.filename,
              page: s.chunk_index,
              url: s.url,
              title: s.title || s.filename,
              source_type: s.source_type || (s.url ? 'web' : 'kb'),
              content: s.content,
              document_id: s.document_id,
              chunk_index: s.chunk_index,
              total_chunks: s.total_chunks,
              index: idx + 1
            }))
            chatStore.updateMessage(lastMsg.id, { sources: mappedSources })
          }
          // 确保 isLoading 被设置为 false，避免消息一直处于加载状态
          chatStore.updateMessage(lastMsg.id, { isLoading: false })
          // 后端 message_id 同步替换前端临时 id：前端先用临时 id 占位渲染，
          // 流结束后用后端正式 ID 替换，确保后续反馈/关联操作能定位到真实消息。
          if (data.message_id && lastMsg.id !== data.message_id) {
            const oldId = lastMsg.id
            const index = chatStore.messages.findIndex(m => m.id === oldId)
            if (index !== -1 && chatStore.messages[index]) {
              chatStore.messages[index].id = data.message_id
            }
          }
          // 新会话创建：首条消息触发后端创建新会话，写入当前会话并 replace 路由 query。
          // 直接赋值，避免 setCurrentSession 清空已渲染的流式消息；
          // 标题先用问题截断占位，后台生成完成后由 title 事件更新。
          if (data.session_id && !chatStore.currentSession) {
            chatStore.currentSession = {
              id: data.session_id,
              title: question.slice(0, 50),
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString()
            }
            router.replace({ path: '/', query: { session: data.session_id } })
          }
          // 刷新会话列表，确保新会话或更新后的会话出现在左侧列表
          queryClient.invalidateQueries({ queryKey: ['sessions'] })
          queryClient.refetchQueries({ queryKey: ['sessions'] })
          searchStatus.value = 'idle'
          // 注意：此处不调用 closeStream()——后端 end 后还会补发 title 事件再关流，
          // 提前 abort 会丢失标题更新
        } else if (data.type === 'error') {
          cancelReasoningRaf()
          searchStatus.value = 'idle'
          closeStream()
          chatStore.updateMessage(lastMsg.id, { content: `⚠ 错误: ${data.error}`, isLoading: false })
        }
      } catch {
        cancelReasoningRaf()
        searchStatus.value = 'idle'
        closeStream()
        if (!receivedContent) {
          const lastMsg = chatStore.messages[chatStore.messages.length - 1]
          if (lastMsg) {
            chatStore.updateMessage(lastMsg.id, { content: '⚠ 服务端响应解析失败', isLoading: false })
          }
        }
      }
    }

    const response = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: requestHeaders,
      body: JSON.stringify(requestBody),
      signal: controller.signal
    })

    if (!response.ok) {
      closeStream()
      searchStatus.value = 'idle'
      let detail = ''
      try {
        const err = await response.json()
        detail = typeof err?.detail === 'string' ? `: ${err.detail}` : ''
      } catch {
        // 非 JSON 错误体，忽略
      }
      chatStore.updateMessage(assistantMsgId, {
        content: `⚠ 请求失败 (${response.status})${detail}`,
        isLoading: false
      })
      return
    }

    if (!response.body) {
      closeStream()
      searchStatus.value = 'idle'
      chatStore.updateMessage(assistantMsgId, { content: '⚠ 服务端不支持流式响应', isLoading: false })
      return
    }

    // 逐块读取 SSE 流：按空行（\n\n）切分事件，提取 "data: " 行
    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    try {
      while (!streamEnded) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        const events = buffer.split('\n\n')
        buffer = events.pop() || ''

        for (const event of events) {
          const dataLine = event.split('\n').find(line => line.startsWith('data:'))
          if (!dataLine) continue
          processEvent(dataLine.slice(5).trim())
        }
      }
    } finally {
      // 释放底层连接（服务端正常结束或中途退出均安全）
      controller.abort()
    }

    // 流结束但未收到 end/error 事件且无内容：等价于旧 EventSource 的 onerror
    if (!streamEnded && !receivedEnd && !receivedContent) {
      cancelReasoningRaf()
      searchStatus.value = 'idle'
      const lastMsg = chatStore.messages[chatStore.messages.length - 1]
      if (lastMsg) {
        chatStore.updateMessage(lastMsg.id, { content: '⚠ 连接断开，无法获取响应', isLoading: false })
      }
    }
  } catch (error) {
    searchStatus.value = 'idle'
    chatStore.addMessage({
      id: generateId(),
      role: 'assistant',
      content: `⚠ 发送失败: ${error instanceof Error ? error.message : '未知错误'}`
    })
  }
}

/** 点击快捷问题时填入输入框并发送。 */
function sendQuickQuestion(question: string): void {
  questionInput.value = question
  sendMessage()
}

/** 重新生成最后一条回答：取最后一条用户问题重发。 */
function regenerateLast(): void {
  if (chatStore.isTyping) return
  const hasLoadingMessage = chatStore.messages.some(m => m.role === 'assistant' && m.isLoading)
  if (hasLoadingMessage) return
  const lastUser = [...chatStore.messages].reverse().find(m => m.role === 'user')
  if (lastUser) void sendMessage(lastUser.content)
}

/** 清空当前对话并重置路由。 */
function clearChat(): void {
  chatStore.clearMessages()
  chatStore.setCurrentSession(null)
  router.replace({ path: '/', query: {} })
}

/** 提交对助手回答的星级反馈。 */
function submitFeedback(messageId: string, rating: number): void {
  feedbackMutation.mutate({
    messageId,
    rating,
    reason: '',
    sessionId: chatStore.currentSession?.id
  }, {
    onSuccess: () => {
      chatStore.updateMessage(messageId, { feedback: { rating } })
      const message = rating >= 4 ? '感谢您的好评！' : (rating <= 2 ? '感谢您的反馈，我们会继续改进' : '感谢您的评价！')
      toast.success('反馈提交成功', message)
    },
    onError: () => {
      toast.error('反馈提交失败', '请稍后重试')
    }
  })
}

/** 消息列表星级反馈事件转发。 */
function onSubmitFeedback(payload: { messageId: string; rating: number }): void {
  submitFeedback(payload.messageId, payload.rating)
}

/** 加载指定会话的历史消息并映射到前端消息结构。 */
async function loadSession(sessionId: string): Promise<void> {
  try {
    interface SessionMessage {
      id?: string
      role: 'user' | 'assistant'
      content: string
      reasoning?: ReasoningStep[]
      thinking?: string
      source_metadata?: RawSourceMeta[]
    }

    interface SessionResponse {
      title?: string
      created_at: string
      updated_at: string
      messages: SessionMessage[]
    }

    const res = await api.get<SessionResponse>(`/sessions/${sessionId}`)
    if (res && res.messages) {
      chatStore.setCurrentSession({
        id: sessionId,
        title: res.title || '未命名对话',
        created_at: res.created_at,
        updated_at: res.updated_at
      })
      const messages: Message[] = res.messages.map((msg) => ({
        id: msg.id || generateId(),
        role: msg.role,
        content: msg.content,
        reasoning: msg.reasoning,
        thinking: msg.thinking,
        sources: msg.source_metadata?.map((meta, idx: number) => ({
          source: meta.filename || meta.document_id || 'unknown',
          score: meta.score || 0,
          document_name: meta.filename,
          page: meta.chunk_index,
          url: meta.url,
          title: meta.title || meta.filename,
          source_type:
            meta.source_type === 'web' || meta.source_type === 'kb'
              ? meta.source_type
              : meta.source === 'web_search' || meta.url
                ? 'web'
                : 'kb',
          content: meta.page_content || meta.content,
          document_id: meta.document_id,
          chunk_index: meta.chunk_index,
          total_chunks: meta.total_chunks,
          index: idx + 1
        })) || [],
        isLoading: false
      }))
      chatStore.setMessages(messages)
    }
  } catch (error) {
    console.error('加载会话失败:', error)
    toast.error('加载历史对话失败', '请稍后重试')
  }
}

onMounted(() => {
  chatStore.loadQuickQuestions()

  const sessionId = route.query.session as string
  if (sessionId) {
    loadSession(sessionId)
  }
})

/** 监听 URL 中 session 参数变化，自动加载对应会话历史。 */
watch(
  () => route.query.session as string | undefined,
  (sessionId) => {
    if (!sessionId) {
      chatStore.setCurrentSession(null)
      return
    }
    if (sessionId !== chatStore.currentSession?.id) {
      loadSession(sessionId)
    }
  }
)
</script>
