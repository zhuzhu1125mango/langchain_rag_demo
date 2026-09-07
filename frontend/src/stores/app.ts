import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '@/utils/axios'

export interface User {
  /** 用户唯一 ID。 */
  id: string
  /** 用户名。 */
  username: string
  /** 邮箱。 */
  email?: string
  /** 头像 URL。 */
  avatar?: string
}

export interface AuthResponse {
  /** JWT access token。 */
  access_token: string
  /** token 类型，固定 "bearer"。 */
  token_type: string
  /** 用户 ID（即资源 owner_id）。 */
  user_id: string
}

const STORAGE_KEY = 'vueuse-dark'
const TOKEN_KEY = 'token'

/**
 * 全局应用状态管理。
 *
 * 负责主题模式（持久化）、当前用户、侧边栏折叠与全局加载状态。
 */
export const useAppStore = defineStore('app', () => {
  // 初始化时从 localStorage 或系统偏好读取主题设置
  const isDark = ref(false)

  const saved = localStorage.getItem(STORAGE_KEY)
  if (saved !== null) {
    isDark.value = saved === 'true'
  } else {
    isDark.value = window.matchMedia('(prefers-color-scheme: dark)').matches
  }

  if (isDark.value) {
    document.documentElement.classList.add('dark')
  } else {
    document.documentElement.classList.remove('dark')
  }

  const theme = computed(() => isDark.value ? 'dark' : 'light')

  /** 设置暗黑模式并同步到 localStorage 与 HTML class。 */
  function setDark(dark: boolean) {
    isDark.value = dark
    localStorage.setItem(STORAGE_KEY, String(dark))
    if (dark) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }

  /** 切换暗黑/浅色模式（基于当前状态取反）。 */
  function toggleDark() {
    setDark(!isDark.value)
  }

  const currentUser = ref<User | null>(null)
  const isLoading = ref(false)
  const sidebarCollapsed = ref(false)

  /** 设置当前登录用户，传 null 表示未登录。 */
  function setUser(user: User | null) {
    currentUser.value = user
  }

  /** 登录：签发 JWT 并写入本地存储。 */
  async function login(username: string, password: string): Promise<User> {
    const data = await api.post<AuthResponse>('/auth/login', { username, password })
    localStorage.setItem(TOKEN_KEY, data.access_token)
    const user: User = { id: data.user_id, username }
    currentUser.value = user
    return user
  }

  /** 注册新用户并直接登录。 */
  async function register(username: string, password: string): Promise<User> {
    const data = await api.post<AuthResponse>('/auth/register', { username, password })
    localStorage.setItem(TOKEN_KEY, data.access_token)
    const user: User = { id: data.user_id, username }
    currentUser.value = user
    return user
  }

  /** 拉取当前认证用户信息（无 token 时静默跳过）。 */
  async function fetchMe(): Promise<void> {
    if (!localStorage.getItem(TOKEN_KEY)) {
      return
    }
    try {
      const data = await api.get<{ user_id: string; username: string }>('/auth/me')
      currentUser.value = { id: data.user_id, username: data.username }
    } catch {
      // 401 已由 axios 拦截器清理 token 并跳转，此处静默
    }
  }

  /** 退出登录：清空用户状态并移除本地 token。 */
  function logout() {
    currentUser.value = null
    localStorage.removeItem(TOKEN_KEY)
  }

  /** 切换侧边栏折叠状态。 */
  function toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  /** 设置全局加载状态。 */
  function setLoading(loading: boolean) {
    isLoading.value = loading
  }

  return {
    isDark,
    toggleDark,
    setDark,
    theme,
    isLoading,
    sidebarCollapsed,
    toggleSidebar,
    setLoading,
    currentUser,
    setUser,
    login,
    register,
    fetchMe,
    logout
  }
})