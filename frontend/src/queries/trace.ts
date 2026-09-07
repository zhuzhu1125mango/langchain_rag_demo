import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

/**
 * 链路追踪 API Query 封装（P1-2 全链路 Trace 可视化）。
 *
 * 基于后端 /traces 接口查询当前用户的单次问答链路记录，
 * 支持按会话/时间范围过滤与分页，详情含分阶段耗时与 token 用量。
 */

/** 分阶段耗时明细条目。 */
export interface TraceStage {
  /** 阶段标识，如 intent_route / kb_retrieval / generate。 */
  name: string
  /** 状态：done / error。 */
  status: string
  /** 耗时（毫秒）。 */
  latency_ms: number
  /** 阶段附加信息（可选）。 */
  detail?: Record<string, unknown>
}

/** token 用量（estimated=true 时为字符估算值）。 */
export interface TraceTokenUsage {
  prompt_tokens?: number | null
  completion_tokens?: number | null
  estimated?: boolean
}

/** 列表摘要条目（不含大体积 JSON）。 */
export interface TraceSummary {
  id: string
  session_id?: string | null
  question: string
  primary_mode?: string | null
  fallback_triggered: boolean
  total_latency_ms?: number | null
  stage_count: number
  token_usage?: TraceTokenUsage | null
  created_at?: string | null
}

/** 链路详情（含分阶段耗时与中间产物）。 */
export interface TraceDetail extends TraceSummary {
  user_id?: string | null
  resolved_question?: string | null
  intent_decision?: Record<string, unknown> | null
  tool_calls?: Array<Record<string, unknown>> | null
  search_results?: Array<Record<string, unknown>> | null
  kb_results?: Array<Record<string, unknown>> | null
  stages?: TraceStage[] | null
  fallback_reason?: string | null
  output_pollution_detected?: boolean
  final_answer?: string | null
}

export interface TraceListParams {
  session_id?: string
  start?: string
  end?: string
  limit?: number
  offset?: number
}

export interface TraceListResponse {
  total: number
  items: TraceSummary[]
}

/** 阶段标识 → 中文展示名。 */
export const STAGE_LABELS: Record<string, string> = {
  context_enhance: '上下文增强',
  datetime_tool: '时间工具',
  intent_route: '意图路由',
  tool_first: '工具调用',
  kb_decision: '知识库决策',
  agent_mode: 'Agent 模式',
  web_search: '联网搜索',
  kb_retrieval: '知识库检索',
  generate: '生成回答'
}

/** 查询链路追踪列表（分页 + 过滤）。 */
export function useTraces(params: MaybeRefOrGetter<TraceListParams>) {
  return useQuery({
    queryKey: ['traces', params],
    queryFn: async (): Promise<TraceListResponse> => {
      const res = await api.get<TraceListResponse>('/traces', { params: toValue(params) })
      return res
    },
    placeholderData: (prev) => prev
  })
}

/** 查询单条链路详情。 */
export function useTraceDetail(id: MaybeRefOrGetter<string | null>) {
  const enabled = computed(() => !!toValue(id))
  return useQuery({
    queryKey: ['trace', id],
    queryFn: async (): Promise<TraceDetail> => {
      const res = await api.get<TraceDetail>(`/traces/${toValue(id)}`)
      return res
    },
    enabled
  })
}
