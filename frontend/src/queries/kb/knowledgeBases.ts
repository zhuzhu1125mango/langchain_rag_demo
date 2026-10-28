import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

export interface KnowledgeBase {
  /** 知识库唯一 ID。 */
  id: string
  /** 知识库名称。 */
  name: string
  /** 描述。 */
  description?: string
  /** 是否为默认知识库。 */
  is_default?: boolean
  /** 文档数量。 */
  document_count?: number
  /** 所属分组名称。 */
  group_name?: string
  /** 所属分组 ID。 */
  group_id?: string
  /** 创建时间（ISO 字符串）。 */
  created_at: string
  /** 更新时间（ISO 字符串）。 */
  updated_at: string
}

export interface KnowledgeBaseGroup {
  /** 分组唯一 ID。 */
  id: string
  /** 分组名称。 */
  name: string
  /** 分组内知识库数量。 */
  kb_count?: number
  /** 分组下的知识库列表。 */
  kbs?: KnowledgeBase[]
}

export interface KnowledgeBaseListResponse {
  items: KnowledgeBase[]
}

/** 获取所有知识库列表。 */

export function useKnowledgeBases() {
  return useQuery({
    queryKey: ['knowledge_bases'],
    queryFn: async (): Promise<KnowledgeBase[]> => {
      // 传大 page_size 一次性获取全部，前端无翻页 UI
      // W6 #53：接口可能返回包装对象或直接返回数组（旧版兼容），
      // 用运行时 Array.isArray 守卫替代 `as unknown as` 双重断言
      const data = await api.get<KnowledgeBaseListResponse | KnowledgeBase[]>('/knowledge_bases/', { params: { page_size: 1000 } })
      if (Array.isArray(data)) {
        return data
      }
      return data.items || []
    },
    staleTime: 0,
    refetchOnWindowFocus: true
  })
}

/** 创建新知识库。 */

export function useCreateKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: Partial<KnowledgeBase>): Promise<KnowledgeBase> => {
      const res = await api.post<KnowledgeBase>('/knowledge_bases/', data)
      return res as KnowledgeBase
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
    }
  })
}

/** 更新知识库信息。 */

export function useUpdateKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: Partial<KnowledgeBase> }) => {
      const res = await api.put<KnowledgeBase>(`/knowledge_bases/${id}`, data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
    }
  })
}

/** 删除知识库。 */

export function useDeleteKnowledgeBase() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (kbId: string) => {
      await api.delete(`/knowledge_bases/${kbId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    }
  })
}

export interface BatchDeleteKnowledgeBasesResult {
  /** 实际删除的知识库数量。 */
  deleted_count: number
  /** 因不存在或无权限而跳过的 ID 列表。 */
  skipped_ids: string[]
}

/** 批量删除知识库。 */

export function useBatchDeleteKnowledgeBases() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (ids: string[]): Promise<BatchDeleteKnowledgeBasesResult> => {
      const data = await api.post<BatchDeleteKnowledgeBasesResult>('/knowledge_bases/batch-delete', { ids })
      return data
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    }
  })
}

/** 获取文档分类列表。 */

export interface KBRecommendation {
  kb_id: string
  relevance_score: number
  matched_chunks: number
}

export interface KBRecommendationRequest {
  question: string
  top_k?: number
}

/** 根据问题推荐相关知识库。 */

export function useRecommendKnowledgeBases() {
  return useMutation({
    mutationFn: async (data: KBRecommendationRequest): Promise<KBRecommendation[]> => {
      const res = await api.post<KBRecommendation[]>('/knowledge_bases/recommend', data)
      return res
    }
  })
}
