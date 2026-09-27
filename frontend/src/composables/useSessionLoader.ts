import { onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useChatStore } from '@/stores/chat'
import { api } from '@/utils/axios'
import { generateId } from '@/utils/id'
import { useToast } from '@/composables/useToast'
import type { Message, ReasoningStep } from '@/queries/chat'
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

  return { loadSession }
}
