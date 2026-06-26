/**
 * Vue Router 路由配置，定义页面导航。
 *
 * 包含对话、知识库管理、历史对话与设置中心等路由，设置中心通过子路由
 * 组织常规设置、分类、标签、学习引擎、A/B 实验与反馈统计等页面，
 * 并在 beforeEach 中根据 meta.title 同步浏览器标题。
 */
import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
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
      }
    ]
  }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.beforeEach((to, _from) => {
  if (to.meta.title) {
    document.title = `${to.meta.title} - RAG 知识库问答系统`
  }
})

export default router
