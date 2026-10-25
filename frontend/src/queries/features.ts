import { useQuery } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

/**
 * 部署级能力开关状态。
 *
 * 后端 /config/info 只读返回；前端用它在设置页与相关功能页
 * 明确展示“已实现但未启用”，避免界面看起来可用但实际不生效。
 */
export type FeatureRisk = 'low' | 'medium' | 'high'

export interface FeatureStatus {
  key: string
  title: string
  enabled: boolean
  scope: string
  risk: FeatureRisk
  env_var: string
  note: string
}

interface SystemInfoResponse {
  features?: FeatureStatus[]
}

/** 读取系统能力开关状态（与 API 密钥信息共用 /config/info）。 */
export function useSystemFeatures() {
  return useQuery({
    queryKey: ['system_features'],
    queryFn: async (): Promise<FeatureStatus[]> => {
      const data = await api.get<SystemInfoResponse>('/config/info')
      return data.features ?? []
    },
    staleTime: 60_000
  })
}

/** 按 key 查找能力；未加载/不存在时返回 undefined。 */
export function findFeature(
  features: FeatureStatus[] | undefined,
  key: string
): FeatureStatus | undefined {
  return features?.find(feature => feature.key === key)
}
