import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

export interface ProcessingConfig {
  chunk_size: number
  chunk_overlap: number
  top_k: number
}

/** 获取处理相关配置。 */

export function useProcessingConfig() {
  return useQuery({
    queryKey: ['processing_config'],
    queryFn: async (): Promise<ProcessingConfig> => {
      const data = await api.get<ProcessingConfig>('/config/processing')
      return data
    },
    staleTime: 5 * 60 * 1000
  })
}

/** 更新处理相关配置。 */

export function useUpdateProcessingConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: Partial<ProcessingConfig>): Promise<ProcessingConfig> => {
      const res = await api.put<ProcessingConfig>('/config/processing', data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['processing_config'] })
      queryClient.invalidateQueries({ queryKey: ['system_config'] })
    }
  })
}

/** 重置系统配置到默认值。 */

export function useResetConfig() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async () => {
      await api.post('/config/reset')
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['processing_config'] })
      queryClient.invalidateQueries({ queryKey: ['system_config'] })
    }
  })
}

// ---------------------------------------------------------------------------
// LLM-Wiki 编译层（Phase 2）：页面列表 / 正文预览 / 全量重编译
// ---------------------------------------------------------------------------
