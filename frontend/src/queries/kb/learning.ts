import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

export interface LearningStats {
  total_executions: number
  successful_learnings: number
  misclassification_count: number
  avg_confidence: number
}

export interface LearningConfig {
  enabled: boolean
  learning_interval_hours: number
  last_learning_time?: string
}

export interface MisclassificationCase {
  id: string
  question: string
  actual_decision: boolean
  correct_decision: boolean
  confidence: number
  created_at: string
}

/** 获取学习统计信息。 */

export function useLearningStats() {
  return useQuery({
    queryKey: ['learning_stats'],
    queryFn: async (): Promise<LearningStats> => {
      const data = await api.get<{ success: boolean; data: LearningStats }>('/learning/stats')
      return data.data
    },
    staleTime: 5 * 60 * 1000
  })
}

/** 获取学习功能配置。 */

export function useLearningConfig() {
  return useQuery({
    queryKey: ['learning_config'],
    queryFn: async (): Promise<LearningConfig> => {
      const data = await api.get<{ success: boolean; data: LearningConfig }>('/learning/config')
      return data.data
    },
    staleTime: 0,
    refetchOnWindowFocus: true
  })
}

/** 获取误判分析案例列表。 */

export function useMisclassificationAnalysis(limit: number = 50) {
  return useQuery({
    queryKey: ['misclassification', limit],
    queryFn: async (): Promise<MisclassificationCase[]> => {
      const data = await api.get<{ success: boolean; data: MisclassificationCase[] }>(
        '/learning/misclassification',
        { params: { limit } }
      )
      return data.data
    },
    staleTime: 10 * 60 * 1000
  })
}

/** 触发学习流程。 */

export function useTriggerLearning() {
  return useMutation({
    mutationFn: async (): Promise<unknown> => {
      const data = await api.post<{ success: boolean; data: unknown }>('/learning/trigger')
      return data.data
    }
  })
}

/** 更新学习功能配置。 */

export function useUpdateLearningConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: Partial<{ enabled: boolean; learning_interval_hours: number }>) => {
      const res = await api.put('/learning/config', data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['learning_config'] })
    }
  })
}
