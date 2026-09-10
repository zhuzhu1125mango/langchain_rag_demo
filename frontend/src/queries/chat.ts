import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

/**
 * 聊天相关 API Query/Mutation 封装。
 *
 * 基于 Vue Query 管理会话、消息、反馈、问题重写、分类、
 * 知识库对比等后端接口的缓存与变更。
 */

export interface Session {
  /** 会话唯一 ID。 */
  id: string
  /** 会话标题。 */
  title: string
  /** 创建时间（ISO 字符串）。 */
  created_at: string
  /** 最近更新时间（ISO 字符串）。 */
  updated_at: string
  /** 最近一条消息内容摘要。 */
  last_message?: string
  /** 会话内消息总数。 */
  message_count?: number
}

export interface MessageSource {
  /** 来源标识，通常为文档 ID 或文件名。 */
  source: string
  /** 检索相关性得分。 */
  score: number
  /** 来源文档名。 */
  document_name?: string
  /** 文档切片索引（页码/块序号）。 */
  page?: number
  /** 网页来源 URL。 */
  url?: string
  /** 来源标题。 */
  title?: string
  /** 来源类型：kb=知识库，web=网页搜索。 */
  source_type?: 'kb' | 'web'
  /** 语料种类：raw=原始文档，wiki=LLM 编译综合页（后端缺省回 'raw'）。 */
  source_kind?: 'raw' | 'wiki'
  /** 命中的文本片段内容。 */
  content?: string
  /** 知识库文档 ID，用于定位文档切片详情（kb 来源点击弹窗必需）。 */
  document_id?: string
  /** 文档切片序号（与 page 同义，来自后端 chunk_index，保留以对齐后端字段）。 */
  chunk_index?: number
  /** 文档总切片数，用于展示 "第 N/M 段"。 */
  total_chunks?: number
  /** 来源序号（从 1 开始，用于编号徽章展示）。 */
  index?: number
}

/** reasoning 步骤的额外结构化元数据。 */
export interface ReasoningStepMetadata {
  /** 来源数量。 */
  sources_count?: number
  /** 识别的主要模式。 */
  primary_mode?: string
  /** 推荐工具列表。 */
  suggested_tools?: string[]
  /** 搜索流程类型。 */
  search_pipeline?: string
  /** 是否需要实时信息。 */
  needs_realtime?: boolean
  /** 改写后的问题文本。 */
  rewritten_question?: string
  /** 调用的工具列表。 */
  tools?: string[]
  /** 其他后端透传字段。 */
  [key: string]: unknown
}

/** 单条 reasoning/搜索过程步骤。 */
export interface ReasoningStep {
  /** 步骤唯一 ID，用于前端 diff/更新。 */
  id: string
  /** 步骤类型，如 intent_routing / web_search / kb_retrieve 等。 */
  step: string
  /** 状态：running / done / failed。 */
  status: 'running' | 'done' | 'failed'
  /** 前端展示标题。 */
  title: string
  /** 前端展示内容。 */
  content: string
  /** 创建时间戳（秒）。 */
  timestamp?: number
  /** 耗时（毫秒），可选。 */
  duration_ms?: number | null
  /** 额外结构化数据。 */
  metadata?: ReasoningStepMetadata
}

export interface Message {
  /** 消息唯一 ID（流式中途可能被后端 message_id 替换）。 */
  id: string
  /** 角色：user=用户，assistant=助手。 */
  role: 'user' | 'assistant'
  /** 消息文本内容。 */
  content: string
  /** 检索来源列表。 */
  sources?: MessageSource[]
  /** 用户反馈，rating 为评分，reason 为可选理由。 */
  feedback?: {
    rating: number
    reason?: string
  }
  /** 是否处于加载/流式接收中。 */
  isLoading?: boolean
  /** 搜索/推理过程步骤列表。 */
  reasoning?: ReasoningStep[]
  /** 模型原始思考内容（深度思考开启时流式接收并持久化，历史回看可展开）。 */
  thinking?: string
}

/** 获取会话列表。 */
export function useSessions() {
  return useQuery({
    queryKey: ['sessions'],
    queryFn: async (): Promise<Session[]> => {
      const res = await api.get<Session[]>('/sessions/')
      return res
    },
    staleTime: 5 * 60 * 1000
  })
}

/** 删除单个会话。 */
export function useDeleteSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (sessionId: string) => {
      await api.delete(`/sessions/${sessionId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    }
  })
}

export interface BatchDeleteResponse {
  deleted_count: number
}

/** 批量删除会话。 */
export function useBatchDeleteSessions() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (ids: string[]): Promise<BatchDeleteResponse> => {
      const res = await api.post<BatchDeleteResponse>('/sessions/batch-delete', { ids })
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    }
  })
}

/** 更新会话标题。 */
export function useUpdateSession() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, title }: { id: string; title: string }) => {
      await api.put(`/sessions/${id}`, { title })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    }
  })
}

/** 提交消息反馈评分。 */
export function useSubmitFeedback() {
  return useMutation({
    mutationFn: async ({ messageId, rating, reason, sessionId }: { messageId: string; rating: number; reason?: string; sessionId?: string }) => {
      const res = await api.post<unknown>(`/chat/messages/${messageId}/feedback`, { rating, reason, session_id: sessionId })
      return res
    }
  })
}

export interface SuggestionsResponse {
  suggestions: string[]
}

/** 获取基于当前输入的问题推荐。 */
export function useGetSuggestions() {
  return async function getSuggestions(
    question?: string,
    sessionId?: string,
    kbIds?: string[]
  ): Promise<string[]> {
    const data = await api.post<SuggestionsResponse>('/chat/suggestions', {
      question,
      session_id: sessionId,
      kb_ids: kbIds
    })
    return data.suggestions
  }
}

export interface QuickQuestionsResponse {
  quick_questions: string[]
}

/** 获取首页快捷问题列表。 */
export function useGetQuickQuestions() {
  return async function getQuickQuestions(): Promise<string[]> {
    const data = await api.get<QuickQuestionsResponse>('/sessions/quick_questions')
    return data.quick_questions
  }
}

export interface RewriteResponse {
  rewritten: string
  original: string
  changes: string
}

/** 重写/优化用户问题表述。 */
export function useRewriteQuestion() {
  return useMutation({
    mutationFn: async (question: string): Promise<RewriteResponse> => {
      const res = await api.post<RewriteResponse>('/chat/rewrite', { question })
      return res
    }
  })
}

export interface QuestionClassification {
  /** 问题类型：事实/观点/操作/解释/比较/探索。 */
  type: 'FACTUAL' | 'OPINION' | 'OPERATIONAL' | 'EXPLANATORY' | 'COMPARATIVE' | 'EXPLORATORY'
  /** 子类型细分。 */
  subtype: string
  /** 分类置信度（0-1）。 */
  confidence: number
  /** 分类说明文本。 */
  description: string
}

export interface ClassifyResponse {
  type: string
  subtype: string
  confidence: number
  description: string
}

/** 对用户问题进行类型分类。 */
export function useClassifyQuestion() {
  return useMutation({
    mutationFn: async (question: string): Promise<QuestionClassification> => {
      const res = await api.post<ClassifyResponse>('/chat/classify', { question })
      return res as QuestionClassification
    }
  })
}

export interface KBComparisonItem {
  /** 该知识库生成的答案。 */
  answer: string
  /** 来源标识列表。 */
  sources: string[]
  /** 命中来源的元数据（文件名、切片索引、内容）。 */
  source_metadata: Array<{
    filename: string
    chunk_index: number
    page_content: string
  }>
  /** 是否给出有效答案。 */
  has_answer: boolean
  /** 错误信息（如检索失败）。 */
  error?: string
  /** 命中文档数量。 */
  doc_count?: number
}

export interface CompareAnalysis {
  /** 综合相似度得分。 */
  similarity_score: number
  /** 一致性等级：high=高，medium=中，low=低，unknown=未知。 */
  consistency: 'high' | 'medium' | 'low' | 'unknown'
  /** 汇总结论。 */
  summary: string
  /** 答案差异点列表。 */
  differences: string[]
  /** 两两知识库对比明细。 */
  comparisons: Array<{
    kb1: string
    kb1_name: string
    kb2: string
    kb2_name: string
    similarity: number
  }>
}

export interface CompareResponse {
  /** 原始问题。 */
  question: string
  /** 各知识库对比结果，键为知识库 ID，_analysis 为综合分析。 */
  comparison: Record<string, KBComparisonItem> & { _analysis?: CompareAnalysis }
}

/** 对多个知识库的答案进行对比分析。 */
export function useCompareKnowledgeBases() {
  return useMutation({
    mutationFn: async ({ question, kbIds }: { question: string; kbIds: string[] }): Promise<CompareResponse> => {
      const res = await api.post<CompareResponse>('/chat/compare', { question, kb_ids: kbIds })
      return res
    }
  })
}
