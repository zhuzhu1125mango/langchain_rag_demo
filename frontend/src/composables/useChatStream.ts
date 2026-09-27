import { onBeforeUnmount } from 'vue'
import { useRouter } from 'vue-router'
import { useQueryClient } from '@tanstack/vue-query'
import { useChatStore } from '@/stores/chat'
import type { MessageSource, ReasoningStep } from '@/queries/chat'
import type ChatMessageList from '@/components/chat/ChatMessageList.vue'
import { getToken } from '@/utils/auth'
import { generateId } from '@/utils/id'
import { mergeReasoningSteps } from '@/utils/reasoning'
import type { Ref } from 'vue'

/**
 * 聊天 SSE 流式对话编排（从 ChatView.vue 抽取，行为不变）。
 *
 * 覆盖：消息发送、fetch 流式读取与 SSE 事件协议分发、reasoning 节流、
 * 在途流控制器（卸载/切换时 abort）、快捷发送与重新生成。
 */

/** 后端来源元数据的原始结构（SSE end 事件与会话历史加载共用）。 */
export interface RawSourceMeta {
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

export interface UseChatStreamOptions {
  questionInput: Ref<string>
  selectedKBs: Ref<string[]>
  useWebSearch: Ref<boolean>
  deepThinking: Ref<boolean>
  searchStatus: Ref<'idle' | 'searching' | 'done' | 'failed'>
  /** 发送成功后需要清空的输入辅助状态（如推荐问题列表） */
  suggestions: Ref<string[]>
  messageListRef: Ref<InstanceType<typeof ChatMessageList> | null>
}

export function useChatStream(options: UseChatStreamOptions) {
  const { questionInput, selectedKBs, useWebSearch, deepThinking, searchStatus, suggestions, messageListRef } = options
  const chatStore = useChatStore()
  const queryClient = useQueryClient()
  const router = useRouter()

  // 当前活动的 SSE 流控制器（D4）：提升到组合函数作用域，组件卸载时统一 abort，
  // 避免切页/卸载后流仍在后台写入已销毁的消息。
  let activeStreamController: AbortController | null = null

  // SSE reasoning 事件节流：同一帧内多次 reasoning 更新合并为一次状态提交，
  // 避免高频时间线更新导致渲染抖动。
  let reasoningRafId: number | null = null

  /** 取消挂起的 reasoning 节流更新。 */
  function cancelReasoningRaf(): void {
    if (reasoningRafId !== null) {
      cancelAnimationFrame(reasoningRafId)
      reasoningRafId = null
    }
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
      const apiKey = localStorage.getItem('api_key')
      const requestHeaders: Record<string, string> = { 'Content-Type': 'application/json' }
      if (apiKey) {
        requestHeaders['X-API-Key'] = apiKey
      }
      const savedToken = getToken()
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
      activeStreamController?.abort()
      activeStreamController = controller
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
          // D4 修复：由此前动态取最后一条改为按本次流创建的目标消息 id 定位。
          // 流式期间若发生会话切换/消息清空，动态取 last 会把增量错写到别的消息上；
          // 用 assistantMsgId 精确定位（end 前的 message_id 替换不影响流期内写入）。
          const lastMsg = chatStore.messages.find(m => m.id === assistantMsgId)
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
        if (activeStreamController === controller) {
          activeStreamController = null
        }
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

  onBeforeUnmount(() => {
    // D4：组件卸载时中止仍在进行的 SSE 流，避免后台继续写入已销毁的响应式消息
    activeStreamController?.abort()
    activeStreamController = null
    cancelReasoningRaf()
  })

  return {
    sendMessage,
    sendQuickQuestion,
    regenerateLast
  }
}
