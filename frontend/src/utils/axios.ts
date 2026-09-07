import axios from 'axios'
import type { AxiosRequestConfig, AxiosInstance, AxiosResponse } from 'axios'

const MAX_RETRIES = 3
const RETRY_DELAY = 2000

/**
 * 全局 Axios 实例。
 *
 * 封装了 baseURL、超时、JWT 认证注入、401 跳转与服务端错误自动重试。
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

// 请求拦截器：自动附加 JWT Token 和 API Key
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    const apiKey = localStorage.getItem('api_key') || import.meta.env.VITE_API_KEY
    if (apiKey) {
      config.headers['X-API-Key'] = apiKey
    }
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器：提取 data、处理 401 与网络错误重试
api.interceptors.response.use(
  (response: AxiosResponse) => {
    return response.data
  },
  async (error) => {
    const { config, response, code } = error

    if (response?.status === 401) {
      // 登录/注册接口自身的 401（用户名或密码错误）不触发清理与跳转
      const url: string = config?.url || ''
      if (!url.startsWith('/auth/login') && !url.startsWith('/auth/register')) {
        localStorage.removeItem('token')
        window.location.href = '/login'
      }
      return Promise.reject(error)
    }

    // 重试条件：连接被拒绝（ECONNREFUSED）、连接超时（ETIMEDOUT）或服务端 5xx，
    // 且该请求尚未记录过重试计数（_retryCount 不存在）。首次进入重试分支时才判定，
    // 后续重试通过 _retryCount 递增控制上限，避免对同一请求无限重试。
    const shouldRetry = (code === 'ECONNREFUSED' || code === 'ETIMEDOUT' ||
                        (response?.status && response.status >= 500)) &&
                       config && !config._retryCount

    if (shouldRetry) {
      // 延迟递增策略：第 n 次重试等待 RETRY_DELAY * n 毫秒（2s/4s/6s），
      // 最多重试 MAX_RETRIES 次，超过后抛出原错误。
      config._retryCount = (config._retryCount || 0) + 1
      if (config._retryCount <= MAX_RETRIES) {
        await new Promise(resolve => setTimeout(resolve, RETRY_DELAY * config._retryCount))
        return api(config)
      }
    }

    return Promise.reject(error)
  }
)

export { api }
