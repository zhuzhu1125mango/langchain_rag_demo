/**
 * Vue Router 路由配置，定义页面导航。
 *
 * 包含对话、知识库管理、历史对话与设置中心等路由，设置中心通过子路由
 * 组织常规设置、分类、标签、学习引擎、A/B 实验与反馈统计等页面，
 * 并在 beforeEach 中根据 meta.title 同步浏览器标题。
 */
import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { getToken, isTokenValid } from '@/utils/auth'
import { tryRefreshToken } from '@/utils/axios'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', public: true }
  },
  {
    path: '/',
    name: 'Chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '对话' }
  },
  {
    path: '/knowledge-base',
    name: 'KnowledgeBase',
    component: () => import('@/views/KnowledgeBaseView.vue'),
    meta: { title: '知识库管理' }
  },
  {
    path: '/history',
    name: 'History',
    component: () => import('@/views/HistoryView.vue'),
    meta: { title: '历史对话' }
  },
  {
    path: '/traces',
    name: 'Traces',
    component: () => import('@/views/TraceView.vue'),
    meta: { title: '链路追踪' }
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置中心' },
    children: [
      {
        path: '',
        name: 'SettingsGeneral',
        component: () => import('@/views/Settings/GeneralView.vue'),
        meta: { title: '常规设置' }
      },
      {
        path: 'categories',
        name: 'SettingsCategories',
        component: () => import('@/views/Settings/CategoryView.vue'),
        meta: { title: '分类管理' }
      },
      {
        path: 'tags',
        name: 'SettingsTags',
        component: () => import('@/views/Settings/TagView.vue'),
        meta: { title: '标签管理' }
      },
      {
        path: 'models',
        name: 'SettingsModels',
        component: () => import('@/views/Settings/ModelServicesView.vue'),
        meta: { title: '模型服务' }
      },
      {
        path: 'learning',
        name: 'SettingsLearning',
        component: () => import('@/views/Settings/LearningView.vue'),
        meta: { title: '学习引擎' }
      },
      {
        path: 'experiments',
        name: 'SettingsExperiments',
        component: () => import('@/views/Settings/ExperimentView.vue'),
        meta: { title: 'A/B实验' }
      },
      {
        path: 'feedback',
        name: 'SettingsFeedback',
        component: () => import('@/views/Settings/FeedbackView.vue'),
        meta: { title: '反馈统计' }
      },
      {
        path: 'api-keys',
        name: 'SettingsApiKeys',
        component: () => import('@/views/Settings/ApiKeysView.vue'),
        meta: { title: 'API 密钥' }
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach(async (to) => {
  if (to.meta.title) {
    document.title = `${to.meta.title} - 知澜`
  }

  // 认证守卫：公开页面直接放行；业务页面要求有效 JWT（未过期）或 API Key。
  // token 校验含 exp 解析（仅查存在性不够——过期/伪造 token 只会换来必然 401）。
  // 已登录（token 有效）用户访问 /login 时回跳对话页。
  if (to.meta.public) {
    if (to.name === 'Login' && isTokenValid(getToken())) {
      return { name: 'Chat' }
    }
    return true
  }
  const hasApiKey = !!localStorage.getItem('api_key')
  // api_key 仅支持 localStorage 中手动设置（自托管 API-Key 模式）；
  // 不再回退 import.meta.env.VITE_API_KEY（会被打包进产物公开泄露）
  let hasToken = isTokenValid(getToken())
  if (!hasToken && !hasApiKey && getToken()) {
    // token 过期（非伪造缺失）：尝试滑动窗口续期（W2-10），成功则放行本次导航，
    // 避免活跃用户在 access token 到期后被守卫踢回登录页
    hasToken = await tryRefreshToken()
  }
  if (!hasToken && !hasApiKey) {
    return { name: 'Login' }
  }
  return true
})

/**
 * 陈旧分片自愈（部署后旧入口引用已删除的 hash 分片）。
 *
 * 现象：重新部署前端后，浏览器若仍持有旧入口（旧标签页常驻或 index.html
 * 走缓存），登录成功 router.push('/') 懒加载 ChatView 时会请求旧 hash 分片，
 * 该文件已被新构建删除 → nginx 兜底返回 text/html → 报
 * "Failed to fetch dynamically imported module"，需手动刷新才能进入首页。
 *
 * 处理：捕获分片加载失败后整页 reload 一次（sessionStorage 守卫防死循环），
 * 刷新即拿到新入口从而自愈（token 在 sessionStorage，登录态不丢）；导航成功
 * 后清除守卫，保证下次部署仍可自愈。
 */
const CHUNK_RELOAD_GUARD = 'chunk-reload-guard'
const CHUNK_LOAD_ERROR_RE =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module|Failed to load module script/i

router.onError((error) => {
  const message = error instanceof Error ? error.message : String(error)
  if (!CHUNK_LOAD_ERROR_RE.test(message)) return
  // 已尝试过整页刷新仍失败：交给错误边界/控制台，不再循环刷新
  if (sessionStorage.getItem(CHUNK_RELOAD_GUARD)) return
  sessionStorage.setItem(CHUNK_RELOAD_GUARD, '1')
  window.location.reload()
})

router.afterEach(() => {
  // 导航成功说明当前入口可用，解除守卫，允许下次部署再次自愈
  sessionStorage.removeItem(CHUNK_RELOAD_GUARD)
})

export default router
