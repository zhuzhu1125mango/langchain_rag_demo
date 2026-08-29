import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'
import { computed, unref } from 'vue'
import { useKBStore } from '@/stores/kb'

/**
 * 知识库相关 API Query/Mutation 封装。
 *
 * 基于 Vue Query 管理知识库、文档、分类、标签、搜索、
 * 推荐、质量评估与知识图谱等接口的缓存与变更。
 */

export interface Document {
  /** 文档唯一 ID。 */
  id: string
  /** 文档标题。 */
  title: string
  /** 文件名。 */
  filename?: string
  /** 文件扩展名/类型。 */
  file_type?: string
  /** 文件大小（字节）。 */
  size?: number
  /** 所属知识库 ID。 */
  kb_id: string
  /** 分类 ID。 */
  category?: string
  /** 分类名称（联表返回）。 */
  category_name?: string
  /** 标签 ID 列表。 */
  tags?: string[]
  /** 通用状态。 */
  status?: string
  /** 处理状态：pending/processing/completed/failed 等。 */
  processing_status?: string
  /** 处理信息或错误描述。 */
  processing_message?: string
  /** 处理进度（0-100）。 */
  processing_progress?: number
  /** 切片数量。 */
  chunks_count?: number
  /** 创建时间（ISO 字符串）。 */
  created_at: string
  /** 更新时间（ISO 字符串）。 */
  updated_at: string
  /** 文档类型标识。 */
  document_type?: string
  /** 文档类型中文标签。 */
  document_type_label?: string
  /** 领域标识。 */
  domain?: string
  /** 领域中文标签。 */
  domain_label?: string
  /** 主题列表。 */
  topics?: string[]
  /** 自动摘要。 */
  summary?: string
  /** 质量评分（0-1）。 */
  quality_score?: number
  /** 质量等级（A/B/C 等）。 */
  quality_grade?: string
}

export interface DocumentListResponse {
  items: Document[]
  total: number
}

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

export interface Category {
  /** 分类唯一 ID。 */
  id: string
  /** 分类名称。 */
  name: string
  /** 描述。 */
  description?: string
  /** 父分类 ID，空表示顶级分类。 */
  parent_id?: string
  /** 排序权重，越小越靠前。 */
  sort_order: number
  /** 分类下文档数量。 */
  count?: number
}

export interface CategoryCreateRequest {
  name: string
  description?: string
  parent_id?: string
  sort_order?: number
}

export interface Tag {
  id: string
  name: string
  color: string
  count?: number
}

export interface TagCreateRequest {
  name: string
  color?: string
}

/** 获取文档列表，默认自动关联当前选中的知识库。 */
export function useDocuments(params: Record<string, unknown> = {}, options: Record<string, unknown> = {}) {
  const kbStore = useKBStore()

  const { autoKB = true, ...queryOptions } = options as { autoKB?: boolean }

  const effectiveParams = computed(() => {
    const result: Record<string, unknown> = {}
    for (const key of Object.keys(params)) {
      result[key] = unref(params[key])
    }

    if (autoKB && kbStore.currentKB?.id) {
      if (!('kb_id' in params)) {
        result.kb_id = kbStore.currentKB.id
      }
    }

    return result
  })

  const enabled = computed((): boolean => {
    if ('enabled' in queryOptions) {
      return typeof queryOptions.enabled === 'function'
        ? (queryOptions.enabled as () => boolean)()
        : Boolean(queryOptions.enabled)
    }
    return autoKB ? !!kbStore.currentKB?.id : true
  })

  return useQuery({
    queryKey: ['documents', effectiveParams],
    queryFn: async ({ queryKey }: { queryKey: unknown[] }): Promise<DocumentListResponse> => {
      const [, queryParams] = queryKey as [string, Record<string, unknown>]
      const data = await api.get<DocumentListResponse | Document[]>('/documents/', { params: queryParams })
      if (Array.isArray(data)) {
        return { items: data, total: data.length }
      }
      return { items: data.items || [], total: data.total || 0 }
    },
    staleTime: 0,
    ...queryOptions,
    enabled
  })
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
      const data = await api.get<KnowledgeBaseListResponse>('/knowledge_bases/', { params: { page_size: 1000 } })
      return data.items || data as unknown as KnowledgeBase[]
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
export function useCategories() {
  return useQuery({
    queryKey: ['categories'],
    queryFn: async (): Promise<Category[]> => {
      const data = await api.get<Category[]>('/categories/')
      return data
    },
    staleTime: 10 * 60 * 1000
  })
}

/** 获取文档标签列表。 */
export function useTags() {
  return useQuery({
    queryKey: ['tags'],
    queryFn: async (): Promise<Tag[]> => {
      const data = await api.get<Tag[]>('/tags/')
      return data
    },
    staleTime: 10 * 60 * 1000
  })
}

/** 创建分类。 */
export function useCreateCategory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: CategoryCreateRequest): Promise<{ id: string; name: string }> => {
      const res = await api.post<{ id: string; name: string }>('/categories/', data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['categories'] })
    }
  })
}

/** 更新分类。 */
export function useUpdateCategory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: CategoryCreateRequest }) => {
      const res = await api.put(`/categories/${id}`, data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['categories'] })
    }
  })
}

/** 删除分类。 */
export function useDeleteCategory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (categoryId: string) => {
      await api.delete(`/categories/${categoryId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['categories'] })
    }
  })
}

/** 创建标签。 */
export function useCreateTag() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: TagCreateRequest): Promise<{ id: string; name: string }> => {
      const res = await api.post<{ id: string; name: string }>('/tags/', data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tags'] })
    }
  })
}

/** 更新标签。 */
export function useUpdateTag() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: TagCreateRequest }) => {
      const res = await api.put(`/tags/${id}`, data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tags'] })
    }
  })
}

/** 删除标签。 */
export function useDeleteTag() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (tagId: string) => {
      await api.delete(`/tags/${tagId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tags'] })
    }
  })
}

/** 删除单个文档。 */
export function useDeleteDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (docId: string) => {
      await api.delete(`/documents/${docId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    }
  })
}

/** 批量删除文档。 */
export function useBatchDeleteDocuments() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (ids: string[]) => {
      await api.post('/documents/batch/delete', { ids })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    }
  })
}

/** 更新文档信息。 */
export function useUpdateDocument() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: Partial<Document> }) => {
      const res = await api.put<Document>(`/documents/${id}`, data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    }
  })
}

export interface SearchResult {
  id: string
  filename: string
  kb_id?: string
  kb_name?: string
  content: string
  score: number
  highlight?: string
}

/** 全文搜索文档内容。 */
export function useSearchDocuments() {
  return async function searchDocuments(query: string, kbId?: string, limit: number = 10): Promise<SearchResult[]> {
    const params: Record<string, unknown> = { query, limit }
    if (kbId) {
      params.kb_id = kbId
    }
    const data = await api.get<SearchResult[]>('/documents/search', { params })
    return data
  }
}

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

export interface DocumentSourceResponse {
  doc_id: string
  filename: string
  kb_id?: string
  kb_name?: string
  chunk_index: number
  total_chunks: number
  content: string
  surrounding_content?: string
  highlight_offset?: number
  highlight_length?: number
}

/** 获取文档指定切片及其上下文内容。 */
export function useGetDocumentSource() {
  return async function getDocumentSource(
    docId: string,
    chunkIndex: number,
    contextChunks: number = 1
  ): Promise<DocumentSourceResponse> {
    const params: Record<string, unknown> = { context_chunks: contextChunks }
    const data = await api.get<DocumentSourceResponse>(`/documents/${docId}/source/${chunkIndex}`, { params })
    return data
  }
}

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

/** 获取反馈列表。 */
export function useFeedbackList(
  sessionId?: string,
  messageId?: string,
  skip: number = 0,
  limit: number = 100
) {
  return useQuery({
    queryKey: ['feedback_list', sessionId, messageId, skip, limit],
    queryFn: async (): Promise<FeedbackItem[]> => {
      const params: Record<string, unknown> = { skip, limit }
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

export interface ProcessingConfig {
  chunk_size: number
  chunk_overlap: number
  top_k: number
}

export interface ModelConfig {
  embedding_model_name: string
  ollama_model_name: string
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

/** 获取模型相关配置。 */
export function useModelConfig() {
  return useQuery({
    queryKey: ['model_config'],
    queryFn: async (): Promise<ModelConfig> => {
      const data = await api.get<ModelConfig>('/config/model')
      return data
    },
    staleTime: 5 * 60 * 1000
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


