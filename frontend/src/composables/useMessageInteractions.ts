import { ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useSubmitFeedback } from '@/queries/chat'
import type { MessageSource } from '@/queries/chat'
import { useToast } from '@/composables/useToast'
import { openExternalUrl } from '@/utils/url'

/**
 * 消息列表交互编排（从 ChatView.vue 抽取，行为不变）。
 *
 * 覆盖：星级反馈提交、来源点击导航（网页外跳 / 文档切片弹窗）。
 */
export function useMessageInteractions() {
  const chatStore = useChatStore()
  const toast = useToast()
  const feedbackMutation = useSubmitFeedback()

  const showSourceModal = ref(false)
  const selectedSourceDocId = ref('')
  const selectedSourceChunkIndex = ref(0)

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

  return {
    onSubmitFeedback,
    navigateToSource,
    showSourceModal,
    selectedSourceDocId,
    selectedSourceChunkIndex,
    closeSourceModal
  }
}
