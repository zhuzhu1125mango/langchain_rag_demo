import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { computed, unref } from 'vue'
import { useKBStore } from '@/stores/kb'
import { api } from '@/utils/axios'

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

export type DocumentListQuery = Record<string, unknown>

/** useDocuments 附加选项。 */

export interface UseDocumentsOptions {
  /** 未手动传 kb_id 时自动关联当前选中知识库。 */
  autoKB?: boolean
}

/** 获取文档列表，默认自动关联当前选中的知识库。 */

export function useDocuments(params: DocumentListQuery = {}, options: UseDocumentsOptions & Record<string, unknown> = {}) {
  const kbStore = useKBStore()

  const { autoKB = true, ...queryOptions } = options

  const effectiveParams = computed(() => {
    const result: DocumentListQuery = {}
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
    // W6 #32：整个 key 用 computed 包裹，hash 输入始终是纯数据（数组+扁平对象），
    // 避免 key 中直接嵌响应式对象导致缓存键稳定性差
    queryKey: computed(() => ['documents', effectiveParams.value]),
    queryFn: async (): Promise<DocumentListResponse> => {
      const data = await api.get<DocumentListResponse | Document[]>('/documents/', { params: effectiveParams.value })
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

/** 全文搜索参数。 */

export interface SearchDocumentsParams {
  query: string
  limit: number
  kb_id?: string
}

/** 全文搜索文档内容。 */

export function useSearchDocuments() {
  return async function searchDocuments(query: string, kbId?: string, limit: number = 10): Promise<SearchResult[]> {
    const params: SearchDocumentsParams = { query, limit }
    if (kbId) {
      params.kb_id = kbId
    }
    const data = await api.get<SearchResult[]>('/documents/search', { params })
    return data
  }
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
    const params: { context_chunks: number } = { context_chunks: contextChunks }
    const data = await api.get<DocumentSourceResponse>(`/documents/${docId}/source/${chunkIndex}`, { params })
    return data
  }
}
