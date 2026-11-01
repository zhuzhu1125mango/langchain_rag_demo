import { onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { api } from '@/utils/axios'
import { generateId } from '@/utils/id'
import { useToast } from '@/composables/useToast'
import type { Message, ReasoningStep } from '@/queries/chat'
import { getActiveChatRun } from '@/queries/chat'
import type { RawSourceMeta } from '@/composables/useChatStream'

/**
 * 会话历史装载（从 ChatView.vue 抽取，行为不变）。
 *
 * 覆盖：按 URL query 中的 session 参数加载历史消息（含 source_metadata →
 * MessageSource 映射），并监听参数变化自动切换/清空当前会话。
 */
export function useSessionLoader() {
  const chatStore = useChatStore()
  const route = useRoute()
  const toast = useToast()

  /** 加载指定会话的历史消息并映射到前端消息结构。 */
  async function loadSession(sessionId: string, recoverInFlight = true): Promise<void> {
    try {
      interface SessionMessage {
        id?: string
        role: 'user' | 'assistant'
        content: string
        reasoning?: ReasoningStep[]
        thinking?: string
        source_metadata?: RawSourceMeta[]
        error?: string
        stopped?: boolean
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
          error: msg.error,
          stopped: msg.stopped,
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
        // 刷新/换设备回来时，该会话可能还有在途生成（AGT-1：生成已与连接解耦）
        if (recoverInFlight) void recoverInFlightRun(sessionId)
      }
    } catch (error) {
      console.error('加载会话失败:', error)
      toast.error('加载历史对话失败', '请稍后重试')
    }
  }

  /**
   * AGT-1：找回在途回答。
   *
   * 服务端不再因断连而停止生成，但助手消息要等生成结束才落库——刷新时直接
   * 拉历史会看到「只有问题、没有回答」，用户会以为回答丢了并重问一遍。
   * 这里先放占位消息，等运行结束后重新拉历史（占位被真实消息替换）。
   *
   * 只做「等 + 重拉」而不重放事件流：刷新场景下用户要的是答案不丢，
   * 逐 token 重放会与占位消息的生命周期纠缠，收益不成正比。
   */
  async function recoverInFlightRun(sessionId: string): Promise<void> {
    let active: { run_id: string | null }
    try {
      active = await getActiveChatRun(sessionId)
    } catch {
      // 查询失败不影响历史展示；最坏情况是这次刷新看不到在途回答
      return
    }
    if (!active.run_id) return

    chatStore.addMessage({ id: generateId(), role: 'assistant', content: '', isLoading: true })
    chatStore.setTyping(true)

    const deadline = Date.now() + 120_000
    try {
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 2000))
        // 用户已切到别的会话：停手，别把消息写到当前会话上
        if (chatStore.currentSession?.id !== sessionId) return
        try {
          const latest = await getActiveChatRun(sessionId)
          if (!latest.run_id) break
        } catch {
          // 单次查询失败（网络抖动）不终止，下一轮再试
        }
      }
    } finally {
      chatStore.setTyping(false)
    }

    if (chatStore.currentSession?.id !== sessionId) return
    // 重新拉历史：真实助手消息（含 stopped / error 标记）替换占位消息
    await loadSession(sessionId, false)
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

  return { loadSession }
}
