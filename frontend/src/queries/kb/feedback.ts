import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

export interface FeedbackStats {
  total_count: number
  positive_count: number
  negative_count: number
  average_rating: number
}

export interface FeedbackItem {
  id: string
  session_id: string
  message_id: string
  rating: number
  reason: string
  created_at: string
}

/** 获取反馈统计数据。 */

export interface FeedbackListParams {
  session_id?: string
  message_id?: string
  skip: number
  limit: number
}

/** 获取反馈列表。 */

export function useFeedbackStats(sessionId?: string) {
  return useQuery({
    queryKey: ['feedback_stats', sessionId],
    queryFn: async (): Promise<FeedbackStats> => {
      const params = sessionId ? { session_id: sessionId } : undefined
      const data = await api.get<FeedbackStats>('/feedback/stats', { params })
      return data
    },
    staleTime: 5 * 60 * 1000
  })
}

/** 反馈列表查询参数。 */

export function useFeedbackList(
  sessionId?: string,
  messageId?: string,
  skip: number = 0,
  limit: number = 100
) {
  return useQuery({
    queryKey: ['feedback_list', sessionId, messageId, skip, limit],
    queryFn: async (): Promise<FeedbackItem[]> => {
      const params: FeedbackListParams = { skip, limit }
      if (sessionId) params.session_id = sessionId
      if (messageId) params.message_id = messageId
      const data = await api.get<FeedbackItem[]>('/feedback/', { params })
      return data
    },
    staleTime: 5 * 60 * 1000
  })
}

/** 删除反馈记录。 */

export function useDeleteFeedback() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (feedbackId: string) => {
      await api.delete(`/feedback/${feedbackId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['feedback_stats'] })
      queryClient.invalidateQueries({ queryKey: ['feedback_list'] })
    }
  })
}
