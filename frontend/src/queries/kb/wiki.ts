import { useQuery, useMutation } from '@tanstack/vue-query'
import { computed, unref, type Ref } from 'vue'
import { api } from '@/utils/axios'

export interface WikiPageSummary {
  /** 页面唯一 ID。 */
  id: string
  /** 页面类型：entity（实体）/ topic（主题）/ index（索引）。 */
  page_type: string
  /** 页面标题。 */
  title: string
  /** 编译版本号。 */
  revision: number
  /** 页面状态。 */
  status: string
  /** 来源文档数量。 */
  source_doc_count: number
  /** 更新时间（ISO 字符串）。 */
  updated_at?: string | null
}

export interface WikiPageListResponse {
  pages: WikiPageSummary[]
}

export interface WikiPageContent {
  id: string
  title: string
  page_type: string
  content: string
}

export interface WikiRebuildResult {
  /** 后台重编译任务 ID（用于 WebSocket 进度跟踪）。 */
  upload_id: string
  message: string
}

/** 获取知识库 Wiki 页面列表。 */

export function useWikiPages(kbId: Ref<string | undefined>) {
  return useQuery({
    queryKey: ['wiki_pages', kbId],
    queryFn: async (): Promise<WikiPageSummary[]> => {
      const id = unref(kbId)
      const data = await api.get<WikiPageListResponse>(`/knowledge_bases/${id}/wiki/pages`)
      return data.pages || []
    },
    enabled: computed(() => !!unref(kbId)),
    staleTime: 30 * 1000
  })
}

/** 获取单个 Wiki 页面正文（预览为低频操作，直接函数调用不走缓存）。 */

export async function fetchWikiPageContent(kbId: string, pageId: string): Promise<WikiPageContent> {
  return api.get<WikiPageContent>(`/knowledge_bases/${kbId}/wiki/pages/${pageId}/content`)
}

/** 触发知识库 Wiki 全量重编译（后台任务，返回 upload_id 供进度跟踪）。 */

export function useRebuildWiki() {
  return useMutation({
    mutationFn: async (kbId: string): Promise<WikiRebuildResult> => {
      return api.post<WikiRebuildResult>(`/knowledge_bases/${kbId}/wiki/rebuild`)
    }
  })
}

// ---------------------------------------------------------------------------
// LLM-Wiki 编译层（Phase 3）：体检报告
// ---------------------------------------------------------------------------

export interface WikiLintIssue {
  rule: string
  level: 'error' | 'warning' | 'info'
  page_id: string
  title: string
  message: string
}

export interface WikiLintReport {
  kb_id: string
  checked_pages: number
  issues: WikiLintIssue[]
}

/** Wiki 体检（规则级零 LLM，同步毫秒级，仅报告不修复；低频操作直接函数调用）。 */

export async function fetchWikiLint(kbId: string): Promise<WikiLintReport> {
  return api.get<WikiLintReport>(`/knowledge_bases/${kbId}/wiki/lint`)
}
