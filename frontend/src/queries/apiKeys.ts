import { useMutation, useQuery, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

/**
 * 用户级 API Key Query 封装（E.6，阶段二 MCP 前置）。
 *
 * 每用户在设置页发放/吊销自己的 API Key，供 MCP 客户端、脚本、CI 以
 * 该用户身份调用 API（owner 隔离与 JWT 一致）。密钥明文仅在创建响应
 * 返回一次，列表接口永不包含明文。
 */

/** 密钥列表条目（后端 ApiKeyItem，仅元数据、无明文）。 */
export interface UserApiKey {
  id: string
  name: string
  key_prefix: string
  created_at?: string | null
  last_used_at?: string | null
  expires_at?: string | null
  revoked_at?: string | null
}

/** 创建成功响应——key 明文仅此一次，前端不得持久化。 */
export interface ApiKeyCreated {
  id: string
  name: string
  key_prefix: string
  key: string
  created_at: string
  expires_at?: string | null
}

export interface ApiKeyCreateInput {
  name: string
  expires_in_days?: number
}

/** /config/info 中与本页相关的字段。 */
export interface ApiKeysConfigInfo {
  user_api_keys_enabled?: boolean
  user_api_keys_max_per_user?: number
}

/** 拉取当前用户密钥列表。 */
export function fetchApiKeys(): Promise<UserApiKey[]> {
  return api.get<UserApiKey[]>('/auth/api-keys')
}

/** 发放新密钥（响应含一次性明文）。 */
export function createApiKey(input: ApiKeyCreateInput): Promise<ApiKeyCreated> {
  return api.post<ApiKeyCreated>('/auth/api-keys', input)
}

/** 软吊销密钥（下一请求即失效）。 */
export function revokeApiKey(id: string): Promise<void> {
  return api.delete<void>(`/auth/api-keys/${id}`)
}

/** 读取系统信息（含 user_api_keys_enabled / max_per_user）。 */
export function fetchApiKeysConfigInfo(): Promise<ApiKeysConfigInfo> {
  return api.get<ApiKeysConfigInfo>('/config/info')
}

/** 密钥列表查询。 */
export function useApiKeys() {
  return useQuery({
    queryKey: ['api-keys'],
    queryFn: fetchApiKeys
  })
}

/** 系统信息查询（开关 + 上限；staleTime 内不重复请求）。 */
export function useApiKeysConfigInfo() {
  return useQuery({
    queryKey: ['config-info'],
    queryFn: fetchApiKeysConfigInfo,
    staleTime: 60_000
  })
}

/** 发放密钥 mutation；成功后刷新列表。 */
export function useCreateApiKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: createApiKey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    }
  })
}

/** 吊销密钥 mutation；成功后刷新列表。 */
export function useRevokeApiKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: revokeApiKey,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    }
  })
}

/** 密钥状态：活跃 / 已过期 / 已吊销。 */
export type ApiKeyStatus = 'active' | 'expired' | 'revoked'

/** 由吊销时间与有效期推导展示状态（时间字段为 ISO 字符串）。 */
export function deriveKeyStatus(item: UserApiKey, now: Date = new Date()): ApiKeyStatus {
  if (item.revoked_at) return 'revoked'
  if (item.expires_at && new Date(item.expires_at).getTime() <= now.getTime()) return 'expired'
  return 'active'
}

/** 有效期天数 → ISO 时间串（undefined/null → null = 永久）。 */
export function buildExpiresAt(days?: number | null): string | null {
  if (!days || days <= 0) return null
  return new Date(Date.now() + days * 86_400_000).toISOString()
}

/** 相对时间文案（中文；超过 30 天回退日期展示）。 */
export function formatRelativeTime(iso?: string | null, now: Date = new Date()): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const diffMs = now.getTime() - then
  if (diffMs < 0) return formatDateShort(iso)
  const minutes = Math.floor(diffMs / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days <= 30) return `${days} 天前`
  return formatDateShort(iso)
}

function formatDateShort(iso: string): string {
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
