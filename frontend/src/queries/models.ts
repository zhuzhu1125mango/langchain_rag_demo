import { useQuery } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

/**
 * 用户侧模型目录（`GET /api/models/available`）。
 *
 * 与 `adminModels.ts` 的分工：那个是管理员配置面（含 provider、单价、密钥提示），
 * 全部需要 `X-Admin-Key`；这里只需要登录，且只返回管理员开放给用户的模型。
 * 两套类型刻意分开而不是复用 `AdminModel`——一旦复用，某天管理面加了
 * 敏感字段，用户面就会「因为类型相同」被顺手带出去。
 */

export interface AvailableModel {
  id: string
  display_name: string
  capabilities: string[]
  context_window: number
  /** 是否是「不传 model_id 时实际使用的模型」，UI 用来标默认 */
  is_default: boolean
}

export interface AvailableModelsResponse {
  models: AvailableModel[]
  /** answer 路由的默认模型 id；路由没配时为空串 */
  default_model_id: string
  /** 配置来源：database / file / legacy-env，仅用于排查，不展示给用户 */
  source: string
}

const availableModelsKey = ['available_models'] as const

/**
 * 可选模型列表。
 *
 * `staleTime` 给 5 分钟：模型开关是管理员低频操作，而对话页每次挂载都拉一遍
 * 没有意义。管理员改了开放范围后，用户端最多 5 分钟可见——这个延迟可接受，
 * 因为真正生效与否由后端校验（不可选会 400），前端列表只决定「能不能点」。
 */
export function useAvailableModels() {
  return useQuery({
    queryKey: availableModelsKey,
    queryFn: async (): Promise<AvailableModelsResponse> => api.get<AvailableModelsResponse>('/models/available'),
    staleTime: 5 * 60 * 1000,
    retry: false
  })
}
