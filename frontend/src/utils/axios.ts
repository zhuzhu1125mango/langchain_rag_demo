import axios from 'axios'
import type { AxiosRequestConfig, AxiosInstance, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { getToken, setToken, clearToken } from '@/utils/auth'

const MAX_RETRIES = 3
const RETRY_DELAY = 2000

/**
 * 全局 Axios 实例。
 *
 * 封装了 baseURL、超时、JWT 认证注入、401 自动刷新（滑动窗口续期）、
 * 刷新失败跳转与服务端错误自动重试。
 */
const api = axios.create({
  baseURL: '/api',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json'
  }
}) as Omit<AxiosInstance, 'get' | 'post' | 'put' | 'delete'> & {
  <T = unknown>(config: AxiosRequestConfig): Promise<T>
  get<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T>
  post<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>
  put<T = unknown>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T>
  delete<T = unknown>(url: string, config?: AxiosRequestConfig): Promise<T>
}

// 请求拦截器：自动附加 JWT Token（sessionStorage）；可选附加手动配置的 API Key
api.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    // 仅读 localStorage 中手动设置的 api_key（自托管 API-Key 模式）；
    // 不再回退 import.meta.env.VITE_API_KEY——VITE_ 变量会被打包进产物公开泄露
    const apiKey = localStorage.getItem('api_key')
    if (apiKey) {
      config.headers['X-API-Key'] = apiKey
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 滑动窗口续期（W2-10）：access token 过期触发 401 时，用原始 axios
// （不走本实例拦截器）调用 /auth/refresh 换发新 token。并发 401 共享
// 同一次在途刷新，避免重复换发；成功存新 token 并重放原请求一次。
let refreshInFlight: Promise<string> | null = null

function refreshAccessToken(currentToken: string): Promise<string> {
  if (!refreshInFlight) {
    refreshInFlight = axios
      .post('/api/auth/refresh', null, {
        headers: { Authorization: `Bearer ${currentToken}` },
        timeout: 15000
      })
      .then((resp) => {
        const newToken = (resp.data as { access_token?: string })?.access_token
        if (!newToken) {
          throw new Error('refresh response missing access_token')
        }
        setToken(newToken)
        return newToken
      })
      .finally(() => {
        refreshInFlight = null
      })
  }
  return refreshInFlight
}

/** 供路由守卫等外部调用的续期入口：持有 token 则尝试滑动窗口续期，成功返回 true。 */
export async function tryRefreshToken(): Promise<boolean> {
  const token = getToken()
  if (!token) return false
  try {
    await refreshAccessToken(token)
    return true
  } catch {
    return false
  }
}

// 响应拦截器：提取 data、处理 401 与网络错误重试
api.interceptors.response.use(
  (response: AxiosResponse) => {
    return response.data
  },
  async (error) => {
    const { config, response, code } = error

    if (response?.status === 401) {
      const url: string = config?.url || ''
      // 登录/注册/刷新接口自身的 401（凭据错误）不触发续期与跳转
      const isAuthEndpoint =
        url.startsWith('/auth/login') || url.startsWith('/auth/register') || url.startsWith('/auth/refresh')
      if (!isAuthEndpoint) {
        const cfg = config as InternalAxiosRequestConfig & { _tokenRefreshed?: boolean }
        const currentToken = getToken()
        // 每个请求只尝试一次续期，防止刷新后仍 401 时死循环
        if (!cfg._tokenRefreshed && currentToken) {
          cfg._tokenRefreshed = true
          try {
            await refreshAccessToken(currentToken)
            return api(cfg)
          } catch {
            // 刷新失败（过期/超窗口/服务端拒绝）→ 走清 token 跳登录
          }
        }
        clearToken()
        window.location.href = '/login'
      }
      return Promise.reject(error)
    }

    // 重试条件：连接被拒绝（ECONNREFUSED）、连接超时（ETIMEDOUT）或服务端 5xx。
    // 重试次数上限由 _retryCount 与 MAX_RETRIES 共同控制：首次失败时 _retryCount
    // 为 0/undefined，仍在限定次数内即可重试（D7 修正：此前用 !config._retryCount
    // 导致首次重试后即为 1，后续失败恒不重试，MAX_RETRIES=3 不可达）。
    const currentRetry = (config as { _retryCount?: number } | undefined)?._retryCount ?? 0
    const isRetryable = code === 'ECONNREFUSED' || code === 'ETIMEDOUT' || (response?.status && response.status >= 500)
    const shouldRetry = Boolean(config && isRetryable && currentRetry < MAX_RETRIES)

    if (shouldRetry) {
      // 延迟递增策略：第 n 次重试等待 RETRY_DELAY * n 毫秒（2s/4s/6s），
      // 最多重试 MAX_RETRIES 次，超过后抛出原错误。
      config._retryCount = (config._retryCount || 0) + 1
      await new Promise(resolve => setTimeout(resolve, RETRY_DELAY * config._retryCount))
      return api(config)
    }

    return Promise.reject(error)
  }
)

export { api }
