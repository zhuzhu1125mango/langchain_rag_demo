import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import type { AxiosResponse } from 'axios'
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'
import { useAppStore } from '@/stores/app'

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
  /** 消息级错误（W6 #41：SSE/请求失败写入此字段渲染专用错误卡片，不污染正文）。 */
  error?: string
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
  /** UX-1：用户主动停止生成（保留已输出内容，不显示错误）。 */
  stopped?: boolean
  /** AM-2：待人工审批的高危工具调用（approval_required 事件）。 */
  approval?: {
    /** 审批 ID（回执接口使用）。 */
    id: string
    /** 工具名。 */
    tool: string
    /** 工具参数预览。 */
    arguments?: Record<string, unknown>
    /** 本地审批状态。 */
    status: 'pending' | 'approved' | 'rejected' | 'expired'
  }
}

/** 会话列表筛选条件（全部可选，都不传时行为与旧版一致）。 */
export interface SessionFilters {
  /** 关键词：匹配会话标题与消息正文。 */
  q?: string
  /** 只返回关联了该知识库的会话。 */
  kb_id?: string
  /** 最近活动日期下界（YYYY-MM-DD，含当天）。 */
  date_from?: string
  /** 最近活动日期上界（YYYY-MM-DD，含当天）。 */
  date_to?: string
}

/** 去掉空值：`q=` 这类空参数后端会当没传，但会让 URL 与缓存键变脏。 */
function compactFilters(filters?: SessionFilters): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(filters ?? {})) {
    if (typeof value === 'string' && value.trim()) out[key] = value.trim()
  }
  return out
}

/**
 * 获取会话列表（仅在已登录时启用，避免未登录路由跳转期的 401 噪音）。
 *
 * `filters` 传响应式值时，筛选条件会进 queryKey——否则改了筛选命中缓存，
 * 界面会一直显示上一次的结果。
 *
 * 注意：这里固定返回「最近一页」，供侧边栏这类只关心最近几条的场景使用；
 * 需要翻完整个历史请用 `useInfiniteSessions`（历史页）。
 */
export function useSessions(filters?: MaybeRefOrGetter<SessionFilters | undefined>) {
  const appStore = useAppStore()
  const params = computed(() => compactFilters(toValue(filters)))
  return useQuery({
    queryKey: computed(() => ['sessions', params.value]),
    queryFn: async (): Promise<Session[]> => {
      const res = await api.get<Session[]>('/sessions/', { params: params.value })
      return res
    },
    staleTime: 5 * 60 * 1000,
    enabled: computed(() => !!appStore.currentUser)
  })
}

/** 历史页每页条数：首屏等待与翻页次数的折中（后端默认 100，前端主动收窄）。 */
export const SESSIONS_PAGE_SIZE = 50

/** 列表接口单页上限，与后端 `SESSION_LIST_MAX_LIMIT` 保持一致。 */
export const SESSIONS_MAX_LIMIT = 500

/** 一页会话；`hasMore` 只按「本页是否装满」推断，避免额外一次 COUNT 查询。 */
export interface SessionsPage {
  items: Session[]
  hasMore: boolean
  nextOffset: number
}

/**
 * 由一页数据算出「是否还有下一页」与下一页偏移。
 *
 * 抽成纯函数是为了能直接测：这里的判定错了不会报错，只会表现为
 * 「加载更多按钮消失」或「一直请求同一页」，都是最难从界面上看出来的故障。
 */
export function buildSessionsPage(
  items: Session[],
  offset: number,
  pageSize = SESSIONS_PAGE_SIZE
): SessionsPage {
  return { items, hasMore: items.length === pageSize, nextOffset: offset + items.length }
}

/**
 * 分页获取会话列表（历史页用）。
 *
 * queryKey 里带的是**筛选条件**而不是页码：页码由 vue-query 的 pageParam 管理。
 * 这样切换筛选就自然得到一条全新的无限查询（从第一页开始），
 * 不会出现「筛选变了但仍停留在第 3 页、显示空列表」。
 */
export function useInfiniteSessions(filters?: MaybeRefOrGetter<SessionFilters | undefined>) {
  const appStore = useAppStore()
  const params = computed(() => compactFilters(toValue(filters)))
  return useInfiniteQuery({
    queryKey: computed(() => ['sessions', 'infinite', params.value]),
    queryFn: async ({ pageParam }): Promise<SessionsPage> => {
      const items = await api.get<Session[]>('/sessions/', {
        params: { ...params.value, skip: pageParam, limit: SESSIONS_PAGE_SIZE }
      })
      return buildSessionsPage(items, pageParam)
    },
    initialPageParam: 0,
    getNextPageParam: (last) => (last.hasMore ? last.nextOffset : undefined),
    staleTime: 5 * 60 * 1000,
    enabled: computed(() => !!appStore.currentUser)
  })
}

/**
 * 取回「当前筛选条件下的全部会话 id」。
 *
 * 批量删除不能只作用于已加载的那一页——否则用户点「删除筛选结果」，
 * 界面清空了，下次刷新又冒出来一半。这里按接口上限翻页，直到取完。
 *
 * `maxPages` 是防呆上限（40 × 500 = 2 万条），避免后端 hasMore 判定异常时死循环。
 */
export async function fetchAllSessionIds(
  filters?: SessionFilters,
  maxPages = 40
): Promise<string[]> {
  const params = compactFilters(filters)
  const ids: string[] = []
  for (let page = 0; page < maxPages; page += 1) {
    const items = await api.get<Session[]>('/sessions/', {
      params: { ...params, skip: page * SESSIONS_MAX_LIMIT, limit: SESSIONS_MAX_LIMIT }
    })
    ids.push(...items.map(item => item.id))
    if (items.length < SESSIONS_MAX_LIMIT) break
  }
  return ids
}

/**
 * 从 Content-Disposition 解析文件名。
 *
 * 后端按 RFC 5987 给了 `filename*=UTF-8''...`，中文标题只有走这条才能正确解码；
 * 解析不出来就返回 null，由调用方回退到「标题 + .md」。
 */
function filenameFromDisposition(disposition?: string): string | null {
  if (!disposition) return null
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  if (encoded?.[1]) {
    try {
      return decodeURIComponent(encoded[1])
    } catch {
      return null
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition)
  return plain?.[1] ?? null
}

/** 导出单条会话为 Markdown 并触发浏览器下载。 */
export async function exportSessionMarkdown(sessionId: string, title?: string): Promise<void> {
  // rawResponse：需要读 Content-Disposition 才能拿到后端生成的文件名（含中文截断规则）
  const response = (await api.get(`/sessions/${sessionId}/export`, {
    params: { format: 'markdown' },
    responseType: 'blob',
    rawResponse: true
  })) as unknown as AxiosResponse<Blob>
  const disposition = response.headers?.['content-disposition'] as string | undefined
  const fallback = `${(title || `session-${sessionId.slice(0, 8)}`).replace(/[\\/:*?"<>|]/g, '')}.md`
  const filename = filenameFromDisposition(disposition) ?? fallback

  const url = URL.createObjectURL(response.data)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // 立刻 revoke 会让部分浏览器取消下载，延迟到下一轮事件循环
  setTimeout(() => URL.revokeObjectURL(url), 0)
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

/** HITL 审批回执（AM-2）：批准/拒绝一次高危工具调用。 */
export async function resolveChatApproval(
  approvalId: string,
  approved: boolean,
  reason?: string
): Promise<void> {
  await api.post(`/chat/approvals/${approvalId}`, { approved, reason })
}

/** AGT-1：停止服务端生成。
 *
 * 生成已与 HTTP 连接解耦，只 abort 本地连接不会让服务端停下——必须调这个，
 * 否则用户点「停止」后服务端仍在烧 LLM，且回到会话还能看到回答继续变长。
 */
export async function stopChatRun(
  runId: string
): Promise<{ run_id: string; stopped: boolean; status: string }> {
  return api.post(`/chat/runs/${runId}/stop`)
}

/** AGT-1：查询会话在途运行（刷新/换设备后重新挂上，从 after 续传）。 */
export async function getActiveChatRun(
  sessionId: string
): Promise<{ run_id: string | null; after?: number; published?: number }> {
  return api.get('/chat/runs/active', { params: { session_id: sessionId } })
}

/** 获取基于当前输入的问题推荐。 */export function useGetSuggestions() {
  return async function getSuggestions(
    question?: string,
    sessionId?: string,
    kbIds?: string[],
    signal?: AbortSignal
  ): Promise<string[]> {
    const data = await api.post<SuggestionsResponse>('/chat/suggestions', {
      question,
      session_id: sessionId,
      kb_ids: kbIds
    }, {
      // 推荐问题是辅助功能且与主回答共用本地 LLM 串行推理：主回答进行中
      // 该请求会排队，10s 快速失败静默降级
      timeout: 10000,
      signal
    })
    return data.suggestions
  }
}

export interface QuickQuestionsResponse {
  quick_questions: string[]
}

/** 获取首页快捷问题列表（低频一次性拉取，直接函数调用不走缓存）。 */
export async function getQuickQuestions(): Promise<string[]> {
  const data = await api.get<QuickQuestionsResponse>('/sessions/quick_questions')
  return data.quick_questions
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
  // W6 #52：后端返回宽泛 string，此处做运行时枚举校验替代直接断言，
  // 未知类型回退 EXPLORATORY，防止脏数据流入 UI 分支
  return useMutation({
    mutationFn: async (question: string): Promise<QuestionClassification> => {
      const res = await api.post<ClassifyResponse>('/chat/classify', { question })
      const validTypes: QuestionClassification['type'][] = [
        'FACTUAL', 'OPINION', 'OPERATIONAL', 'EXPLANATORY', 'COMPARATIVE', 'EXPLORATORY'
      ]
      return {
        type: validTypes.includes(res.type as QuestionClassification['type'])
          ? (res.type as QuestionClassification['type'])
          : 'EXPLORATORY',
        subtype: res.subtype,
        confidence: res.confidence,
        description: res.description
      }
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
