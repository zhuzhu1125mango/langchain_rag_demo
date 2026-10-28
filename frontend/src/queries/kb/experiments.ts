import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

export interface Experiment {
  /** 实验唯一 ID。 */
  id: string
  /** 实验名称。 */
  name: string
  /** 描述。 */
  description?: string
  /** 状态：created/running/stopped 等。 */
  status: string
  /** 变体列表，weight 为流量占比（0-1）。 */
  variants?: Array<{ id: string; name: string; weight: number }>
  /** 监控指标名称列表。 */
  metrics?: string[]
  /** 创建时间（ISO 字符串）。 */
  created_at: string
  /** 更新时间（ISO 字符串）。 */
  updated_at: string
  /** 启动时间（ISO 字符串）。 */
  started_at?: string
  /** 结束时间（ISO 字符串）。 */
  ended_at?: string
}

export interface ExperimentCreateRequest {
  name: string
  description?: string
  variants?: Array<{ id: string; name: string; weight: number }>
  metrics?: string[]
}

export interface ExperimentResult {
  /** 实验唯一 ID。 */
  experiment_id: string
  /** 各变体的统计结果，metrics 为指标名到均值/标准差/样本量的映射。 */
  variant_results: Array<{
    variant_id: string
    variant_name: string
    sample_size: number
    metrics: Record<string, { mean: number; std: number; count: number }>
  }>
  /** 获胜变体 ID（存在显著差异时返回）。 */
  winner_variant_id?: string
  /** 统计置信度（0-1）。 */
  confidence_level?: number
}

/** 获取实验列表。 */

export function useExperiments(status?: string) {
  return useQuery({
    queryKey: ['experiments', status],
    queryFn: async (): Promise<Experiment[]> => {
      const params = status ? { status } : undefined
      const data = await api.get<{ success: boolean; data: Experiment[] }>('/experiments/', { params })
      return data.data
    },
    staleTime: 5 * 60 * 1000
  })
}

/** 创建 A/B 实验。 */

export function useCreateExperiment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: ExperimentCreateRequest): Promise<{ experiment_id: string }> => {
      const res = await api.post<{ success: boolean; experiment_id: string }>('/experiments/', data)
      return { experiment_id: res.experiment_id }
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
    }
  })
}

/** 启动实验。 */

export function useStartExperiment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (experimentId: string) => {
      await api.post(`/experiments/${experimentId}/start`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
    }
  })
}

/** 停止实验。 */

export function useStopExperiment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (experimentId: string) => {
      await api.post(`/experiments/${experimentId}/stop`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
    }
  })
}

/** 获取实验结果。 */

export function useExperimentResult(experimentId: string) {
  return useQuery({
    queryKey: ['experiment_result', experimentId],
    queryFn: async (): Promise<ExperimentResult> => {
      const data = await api.get<{ success: boolean; data: ExperimentResult }>(
        `/experiments/${experimentId}/result`
      )
      return data.data
    },
    staleTime: 5 * 60 * 1000,
    enabled: !!experimentId
  })
}

/** 分析实验结果。 */

export function useAnalyzeExperiment() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (experimentId: string): Promise<ExperimentResult> => {
      const data = await api.post<{ success: boolean; data: ExperimentResult }>(
        `/experiments/${experimentId}/analyze`
      )
      return data.data
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['experiment_result'] })
    }
  })
}

export interface BatchDeleteExperimentsResult {
  success_count: number
  failed_count: number
  failed: Array<{ experiment_id: string; reason: string }>
  message: string
}

/** 批量删除实验。 */

export function useDeleteExperiments() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (experimentIds: string[]): Promise<BatchDeleteExperimentsResult> => {
      const data = await api.delete<{ success: boolean; data: BatchDeleteExperimentsResult }>(
        '/experiments/batch',
        { data: { experiment_ids: experimentIds } }
      )
      return data.data
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['experiments'] })
      queryClient.invalidateQueries({ queryKey: ['experiment_result'] })
    }
  })
}
