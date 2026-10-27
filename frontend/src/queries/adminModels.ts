import { computed, toValue, type MaybeRefOrGetter } from 'vue'
import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

/** P2 模型服务管理 API（仅管理员）。Key 只写不读，前端不持久化 Provider Key。 */

export interface AdminProvider {
  id: string
  name: string
  provider_type: string
  base_url?: string | null
  api_key_env?: string | null
  api_key_required: boolean
  has_api_key: boolean
  api_key_hint?: string
  headers: Record<string, string>
  timeout_s: number
  external: boolean
  enabled: boolean
}

export interface AdminModel {
  id: string
  provider_id: string
  model_name: string
  display_name: string
  capabilities: string[]
  context_window: number
  max_output_tokens: number
  embedding_dimension: number
  temperature?: number | null
  input_cost_per_1k?: number | null
  output_cost_per_1k?: number | null
  price_currency?: string
  enabled: boolean
}

export interface AdminRoute {
  task: string
  primary_model_id: string
  primary_model_name?: string
  fallback_model_ids: string[]
  fallback_model_names?: string[]
  reasoning: string
  temperature?: number | null
  max_output_tokens?: number | null
  timeout_s?: number | null
  allow_external_fallback: boolean
}

export interface AdminSnapshot {
  providers: AdminProvider[]
  models: AdminModel[]
  routes: AdminRoute[]
}

export interface ProviderPayload {
  name?: string
  provider_type?: string
  base_url?: string
  api_key?: string
  api_key_env?: string
  api_key_required?: boolean
  headers?: Record<string, string>
  timeout_s?: number
  external?: boolean
  enabled?: boolean
}

export interface ModelPayload {
  provider_id?: string
  model_name?: string
  display_name?: string
  capabilities?: string[]
  context_window?: number
  max_output_tokens?: number
  embedding_dimension?: number
  temperature?: number | null
  input_cost_per_1k?: number | null
  output_cost_per_1k?: number | null
  price_currency?: string
  enabled?: boolean
}

export interface RoutePayload {
  task: string
  primary_model_id: string
  fallback_model_ids: string[]
  reasoning: string
  temperature?: number | null
  max_output_tokens?: number | null
  timeout_s?: number | null
  allow_external_fallback: boolean
}

export interface ConnectionTestResult {
  ok: boolean
  models: string[]
  status_code?: number
  error?: string
}

export interface ModelUsageRow {
  provider_id: string
  model_id: string
  model_name: string
  task: string
  currency: string
  calls: number
  errors: number
  input_tokens: number
  output_tokens: number
  avg_latency_ms: number
  priced_calls: number
  unpriced_calls: number
  estimated_cost: number
  last_error?: string
}

export interface ProviderHealthRow {
  provider_id: string
  state: string
  consecutive_failures: number
  total_calls: number
  total_failures: number
  total_skipped: number
  opened_at?: number | null
  last_failure_at?: number | null
  last_error?: string
}

export interface ModelUsageResponse {
  usage: ModelUsageRow[]
  health: ProviderHealthRow[]
  days: number
}

export interface IndexProfileInfo {
  profile_id: string
  provider_id?: string
  model_id?: string
  model_name: string
  display_name?: string
  dimension: number
}

export interface IndexStatus {
  collection: string
  indexed: IndexProfileInfo | null
  target: IndexProfileInfo
  in_sync: boolean
  needs_rebuild: boolean
  can_rollback: boolean
  status: string
  chunk_count: number
  backup_collection?: string | null
  last_error?: string
}

const ADMIN_KEY_STORAGE = 'rag_admin_key'

export function getAdminKey(): string {
  return sessionStorage.getItem(ADMIN_KEY_STORAGE) || ''
}

export function setAdminKey(value: string): void {
  if (value) sessionStorage.setItem(ADMIN_KEY_STORAGE, value)
  else sessionStorage.removeItem(ADMIN_KEY_STORAGE)
}

function adminHeaders(): Record<string, string> {
  const key = getAdminKey()
  return key ? { 'X-Admin-Key': key } : {}
}

const snapshotKey = ['admin_model_snapshot'] as const
const indexStatusKey = ['admin_model_index_status'] as const

export function useAdminModelSnapshot() {
  return useQuery({
    queryKey: snapshotKey,
    queryFn: async (): Promise<AdminSnapshot> => api.get<AdminSnapshot>('/admin/models/snapshot', { headers: adminHeaders() }),
    staleTime: 10_000,
    retry: false
  })
}

function useInvalidate() {
  const queryClient = useQueryClient()
  return () => queryClient.invalidateQueries({ queryKey: snapshotKey })
}

/** Embedding 索引画像：当前索引 vs 待应用配置、重建中状态。 */
export function useIndexStatus() {
  return useQuery({
    queryKey: indexStatusKey,
    queryFn: async (): Promise<IndexStatus> =>
      api.get<IndexStatus>('/admin/models/index/status', { headers: adminHeaders() }),
    staleTime: 5_000,
    retry: false,
    refetchInterval: query => (query.state.data?.status === 'rebuilding' ? 5_000 : false)
  })
}

function useIndexInvalidate() {
  const queryClient = useQueryClient()
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: indexStatusKey }),
      queryClient.invalidateQueries({ queryKey: snapshotKey })
    ])
}

export function useRebuildIndex() {
  const invalidate = useIndexInvalidate()
  return useMutation({
    mutationFn: async (force: boolean) =>
      api.post('/admin/models/index/rebuild', {}, { params: { force }, headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useRollbackIndex() {
  const invalidate = useIndexInvalidate()
  return useMutation({
    mutationFn: async () =>
      api.post<IndexStatus>('/admin/models/index/rollback', {}, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

/** 模型用量与成本（持久化最近 N 天 + 当前进程未落库增量）。 */
export function useModelUsage(days: MaybeRefOrGetter<number> = 7) {
  return useQuery({
    queryKey: computed(() => ['admin_model_usage', toValue(days)]),
    queryFn: async (): Promise<ModelUsageResponse> =>
      api.get<ModelUsageResponse>('/admin/models/usage', {
        params: { days: toValue(days) },
        headers: adminHeaders()
      }),
    staleTime: 15_000,
    retry: false
  })
}

export function useCreateProvider() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async (payload: ProviderPayload) =>
      api.post<AdminProvider>('/admin/models/providers', payload, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useUpdateProvider() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async ({ id, payload }: { id: string; payload: ProviderPayload }) =>
      api.patch<AdminProvider>(`/admin/models/providers/${id}`, payload, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useDeleteProvider() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async (id: string) => api.delete(`/admin/models/providers/${id}`, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useTestProvider() {
  return useMutation({
    mutationFn: async (payload: ProviderPayload): Promise<ConnectionTestResult> =>
      api.post<ConnectionTestResult>('/admin/models/providers/test', payload, { headers: adminHeaders() })
  })
}

export function useTestSavedProvider() {
  return useMutation({
    mutationFn: async (id: string): Promise<ConnectionTestResult> =>
      api.post<ConnectionTestResult>(`/admin/models/providers/${id}/test`, {}, { headers: adminHeaders() })
  })
}

export function useDiscoverModels() {
  return useMutation({
    mutationFn: async (id: string): Promise<ConnectionTestResult> =>
      api.get<ConnectionTestResult>(`/admin/models/providers/${id}/models`, { headers: adminHeaders() })
  })
}

export function useCreateModel() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async (payload: ModelPayload) =>
      api.post<AdminModel>('/admin/models/deployments', payload, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useUpdateModel() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async ({ id, payload }: { id: string; payload: ModelPayload }) =>
      api.patch<AdminModel>(`/admin/models/deployments/${id}`, payload, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useDeleteModel() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async (id: string) => api.delete(`/admin/models/deployments/${id}`, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useSaveRoutes() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async (routes: RoutePayload[]) =>
      api.put<AdminSnapshot>('/admin/models/routes', routes, { headers: adminHeaders() }),
    onSuccess: invalidate
  })
}

export function useApplyRegistry() {
  const invalidate = useInvalidate()
  return useMutation({
    mutationFn: async () =>
      api.post<{ source: string; providers: number; models: number; routes: number }>(
        '/admin/models/apply',
        {},
        { headers: adminHeaders() }
      ),
    onSuccess: invalidate
  })
}
