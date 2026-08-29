<template>
  <div class="flex-1 flex h-full bg-gray-50 dark:bg-dark-900 relative">
    <!-- 会话列表面板 -->
    <SessionListPanel
      ref="sessionListRef"
      :show="showHistoryPanel"
      :sessions="sessionsData"
      :current-session-id="chatStore.currentSession?.id"
      @create-new-session="createNewSession"
      @switch-session="switchSession"
      @delete-session="deleteSession"
      @batch-delete="batchDeleteSessions"
    />

    <!-- 答案对比侧边面板 -->
    <CompareAnswerPanel
      :show="showComparePanel"
      :is-comparing="isComparing"
      :compare-result="compareResult"
      :get-kb-name="getKBName"
      @close="showComparePanel = false"
    />

    <button
      @click="showHistoryPanel = !showHistoryPanel"
      :class="[
        'absolute left-0 top-1/2 -translate-y-1/2 z-20 flex items-center justify-center w-6 h-16 bg-white dark:bg-dark-800 border border-gray-200 dark:border-dark-600 rounded-r-lg hover:bg-gray-50 dark:hover:bg-dark-700 transition-all shadow-sm',
        showHistoryPanel ? 'translate-x-[288px]' : 'translate-x-0'
      ]"
    >
      <ChevronRight
        v-if="!showHistoryPanel"
        class="w-4 h-4 text-gray-500"
      />
      <ChevronLeft
        v-else
        class="w-4 h-4 text-gray-500"
      />
    </button>

    <div class="flex-1 flex flex-col h-full">
      <header class="bg-white dark:bg-dark-800 border-b border-gray-200 dark:border-dark-600 px-6 py-4">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="text-xl font-semibold text-gray-800 dark:text-white">智能助手</h1>
            <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">基于知识库的智能问答系统</p>
          </div>
          <div class="flex items-center gap-3">
            <button
              @click="clearChat"
              class="flex items-center gap-2 px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
            >
              <Trash2 class="w-4 h-4" />
              <span>清空对话</span>
            </button>
            <button
              @click="showKBSelector = !showKBSelector"
              class="flex items-center gap-2 px-4 py-2 text-sm text-primary-600 dark:text-primary-400 bg-primary-50 dark:bg-primary-900/30 hover:bg-primary-100 dark:hover:bg-primary-900/40 rounded-lg transition-colors"
            >
              <BookOpen class="w-4 h-4" />
              <span>{{ selectedKBs.length > 0 ? `${selectedKBs.length}个已选` : '选择知识库' }}</span>
            </button>
            <button
              @click="handleCompareAnswers"
              :disabled="selectedKBs.length < 2 || !questionInput.trim()"
              :class="[
                'flex items-center gap-2 px-4 py-2 text-sm rounded-lg transition-colors',
                selectedKBs.length >= 2 && questionInput.trim()
                  ? 'bg-purple-500 text-white hover:bg-purple-600'
                  : 'bg-gray-100 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
              ]"
            >
              <GitCompare class="w-4 h-4" />
              <span>对比答案</span>
            </button>
            <button
              @click="useWebSearch = !useWebSearch"
              :class="[
                'flex items-center gap-2 px-4 py-2 text-sm rounded-lg transition-colors',
                useWebSearch
                  ? 'bg-blue-500 text-white hover:bg-blue-600'
                  : 'bg-gray-100 dark:bg-dark-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-dark-600'
              ]"
            >
              <Globe class="w-4 h-4" />
              <span>{{
                searchStatus === 'searching'
                  ? '搜索中...'
                  : searchStatus === 'failed'
                    ? '搜索失败'
                    : searchStatus === 'done'
                      ? '搜索完成'
                      : useWebSearch
                        ? '联网搜索开启'
                        : '联网搜索'
              }}</span>
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
        @navigate-source="navigateToSource"
        @submit-feedback="onSubmitFeedback"
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
        :quick-questions="chatStore.quickQuestions"
        :is-typing="chatStore.isTyping"
        :get-kb-name="getKBName"
        @send="sendMessage"
        @send-quick-question="sendQuickQuestion"
        @rewrite-question="handleRewriteQuestion"
        @use-rewritten-question="useRewrittenQuestion"
        @clear-rewrite="rewriteResult = null"
        @input-change="onInputChange"
        @toggle-kb-id="toggleKBById"
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
import { ref, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useQueryClient } from '@tanstack/vue-query'
import { useChatStore } from '@/stores/chat'
import { useSessions, useDeleteSession, useBatchDeleteSessions, useSubmitFeedback, useGetSuggestions, useRewriteQuestion, useClassifyQuestion, useCompareKnowledgeBases } from '@/queries/chat'
import type { Session, Message, MessageSource, CompareResponse, ReasoningStep } from '@/queries/chat'
import { useKnowledgeBases, useRecommendKnowledgeBases } from '@/queries/kb'
import type { KBRecommendation } from '@/queries/kb'
import { Trash2, BookOpen, GitCompare, Globe, ChevronLeft, ChevronRight } from '@lucide/vue'
import DocumentSourceModal from '@/components/DocumentSourceModal.vue'
import SessionListPanel from '@/components/chat/SessionListPanel.vue'
import CompareAnswerPanel from '@/components/chat/CompareAnswerPanel.vue'
import KnowledgeBaseSelector from '@/components/chat/KnowledgeBaseSelector.vue'
import ChatMessageList from '@/components/chat/ChatMessageList.vue'
import ChatInputArea from '@/components/chat/ChatInputArea.vue'
import { useToast } from '@/composables/useToast'
import { api } from '@/utils/axios'
import { generateId } from '@/utils/id'
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
const suggestions = ref<string[]>([])
const isGeneratingSuggestions = ref(false)
const showHistoryPanel = ref(true)
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

/** 将 reasoning 步骤合并到现有列表：同 step 且同 id 的用新状态覆盖，新的追加。 */
function mergeReasoningSteps(existing: ReasoningStep[], incoming: ReasoningStep[]): ReasoningStep[] {
  const map = new Map<string, ReasoningStep>()
  existing.forEach(s => map.set(s.id, s))
  incoming.forEach(s => map.set(s.id, s))
  return Array.from(map.values())
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
const sessionListRef = ref<InstanceType<typeof SessionListPanel> | null>(null)
const messageListRef = ref<InstanceType<typeof ChatMessageList> | null>(null)

const feedbackMutation = useSubmitFeedback()
const getSuggestions = useGetSuggestions()
const rewriteQuestionMutation = useRewriteQuestion()
const classifyQuestionMutation = useClassifyQuestion()
const compareKBsMutation = useCompareKnowledgeBases()
const recommendKBsMutation = useRecommendKnowledgeBases()

const { data: knowledgeBases } = useKnowledgeBases()
const { data: sessionsData } = useSessions()
const deleteSessionMutation = useDeleteSession()
const batchDeleteMutation = useBatchDeleteSessions()


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

/** 发送用户问题，通过 SSE 接收流式回答。 */
async function sendMessage(): Promise<void> {
  if (!questionInput.value.trim()) return

  const hasLoadingMessage = chatStore.messages.some(m => m.role === 'assistant' && m.isLoading)
  if (hasLoadingMessage) return

  if (useWebSearch.value) {
    searchStatus.value = 'searching'
  }

  const question = questionInput.value.trim()
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
      use_web_search: useWebSearch.value
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
    let streamEnded = false
    const closeStream = () => {
      if (!streamEnded) {
        streamEnded = true
        controller.abort()
      }
    }

    // SSE 事件协议：
    //   - content：data.content 为增量文本片段，累加到助手消息内容上
    //   - reasoning：搜索/思考过程结构化数据，供前端折叠面板展示
    //   - search_status：向后兼容，映射为 reasoning 步骤并更新搜索按钮状态
    //   - end：流结束，data.sources 为来源列表，data.message_id 为后端正式消息 ID，
    //          data.session_id/data.title 用于新会话创建与标题更新，data.reasoning 为完整推理过程
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

        if (data.type === 'reasoning') {
          const step = data as ReasoningStep
          const current = lastMsg.reasoning || []
          const merged = mergeReasoningSteps(current, [step])
          chatStore.updateMessage(lastMsg.id, { reasoning: merged })
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
          // 强制 flush 可能挂起的 reasoning 更新
          cancelReasoningRaf()
          if (data.reasoning && Array.isArray(data.reasoning)) {
            const finalReasoning = data.reasoning as ReasoningStep[]
            const current = lastMsg.reasoning || []
            chatStore.updateMessage(lastMsg.id, { reasoning: mergeReasoningSteps(current, finalReasoning) })
          }
          if (data.sources) {
            const mappedSources: MessageSource[] = data.sources.map((s: any, idx: number) => ({
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
          // 分支：新会话创建 / 已有会话标题更新
          if (data.session_id && !chatStore.currentSession) {
            // 首条消息触发后端创建新会话，写入当前会话并 replace 路由 query
            // 直接赋值，避免 setCurrentSession 清空已渲染的流式消息
            chatStore.currentSession = {
              id: data.session_id,
              title: data.title || question.slice(0, 50),
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString()
            }
            router.replace({ path: '/', query: { session: data.session_id } })
          } else if (data.title && chatStore.currentSession) {
            // 后端生成了新标题，更新当前会话并刷新列表
            chatStore.updateCurrentSessionTitle(data.title)
          }
          // 刷新会话列表，确保新会话或更新后的会话出现在左侧列表
          queryClient.invalidateQueries({ queryKey: ['sessions'] })
          queryClient.refetchQueries({ queryKey: ['sessions'] })
          searchStatus.value = 'idle'
          closeStream()
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
    if (!streamEnded && !receivedContent) {
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

/** 清空当前对话并重置路由。 */
function clearChat(): void {
  chatStore.clearMessages()
  chatStore.setCurrentSession(null)
  router.replace({ path: '/', query: {} })
}

/** 创建新会话并切换到该会话。 */
async function createNewSession(): Promise<void> {
  try {
    const res = await api.post<{ id: string; title: string }>('/sessions/', { title: '新会话' })
    if (res && res.id) {
      chatStore.setCurrentSession({
        id: res.id,
        title: res.title,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString()
      })
      chatStore.clearMessages()
      router.replace({ path: '/', query: { session: res.id } })
      // 刷新会话列表，确保新建会话后立即显示
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
      queryClient.refetchQueries({ queryKey: ['sessions'] })
    }
  } catch (error) {
    console.error('创建会话失败:', error)
    toast.error('创建会话失败', '请稍后重试')
  }
}

/** 切换当前会话并加载历史消息。 */
async function switchSession(session: Session): Promise<void> {
  if (chatStore.currentSession?.id === session.id) return
  router.replace({ path: '/', query: { session: session.id } })
  await loadSession(session.id)
}

/** 删除单个会话，若删除的是当前会话则清空界面。 */
function deleteSession(session: Session): void {
  if (confirm(`确定要删除对话 "${session.title || '未命名对话'}" 吗？`)) {
    deleteSessionMutation.mutate(session.id, {
      onSuccess: () => {
        if (chatStore.currentSession?.id === session.id) {
          chatStore.clearMessages()
          chatStore.setCurrentSession(null)
          router.replace({ path: '/', query: {} })
        }
        toast.success('删除成功', '会话已删除')
      },
      onError: () => {
        toast.error('删除失败', '请稍后重试')
      }
    })
  }
}

/** 批量删除选中的会话。 */
async function batchDeleteSessions(ids: string[]): Promise<void> {
  if (ids.length === 0) return
  if (!confirm(`确定要删除选中的 ${ids.length} 个会话吗？此操作不可恢复。`)) return

  const hasCurrent = chatStore.currentSession?.id && ids.includes(chatStore.currentSession.id)

  try {
    const res = await batchDeleteMutation.mutateAsync(ids)
    queryClient.refetchQueries({ queryKey: ['sessions'] })

    if (hasCurrent) {
      chatStore.clearMessages()
      chatStore.setCurrentSession(null)
      router.replace({ path: '/', query: {} })
    }

    toast.success('删除成功', `已删除 ${res.deleted_count} 个会话`)
  } catch (error) {
    console.error('批量删除会话失败:', error)
    toast.error('删除失败', '请稍后重试')
  } finally {
    sessionListRef.value?.resetBatchMode()
  }
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
      source_metadata?: {
        filename?: string
        document_id?: string
        chunk_index?: number
        url?: string
        title?: string
        source?: string
      }[]
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
        sources: msg.source_metadata?.map((meta: any, idx: number) => ({
          source: meta.filename || meta.document_id || 'unknown',
          score: meta.score || 0,
          document_name: meta.filename,
          page: meta.chunk_index,
          url: meta.url,
          title: meta.title || meta.filename,
          source_type: meta.source_type || (meta.source === 'web_search' || meta.url ? 'web' : 'kb'),
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
