/**
 * E2E 路由 mock 基建。
 *
 * 通过 page.route 拦截 /api/** 与 page.routeWebSocket 拦截上传进度/通知 WS，
 * 使三条核心链路 E2E 不依赖真实后端与 Ollama。mock 数据字段与
 * src/queries/*.ts 的 TS 接口对齐（snake_case）。
 *
 * 路由注册顺序：先注册 catch-all，再注册具体路由（Playwright 以
 * 「最近注册者优先」的顺序匹配）。
 */
import type { Page } from '@playwright/test'

/** 知识库 mock 数据（KnowledgeBase 接口）。 */
export interface MockKB {
  id: string
  name: string
  description?: string
  is_default?: boolean
  document_count?: number
  created_at: string
  updated_at: string
}

/** 会话 mock 数据（Session 接口）。 */
export interface MockSession {
  id: string
  title: string
  created_at: string
  updated_at: string
  last_message?: string
  message_count?: number
}

/** 测试共享状态：创建/删除知识库的用例通过修改 kbs 数组实现有状态 mock。 */
export interface MockState {
  kbs: MockKB[]
}

export function createMockState(): MockState {
  return {
    kbs: [
      {
        id: 'kb-1',
        name: '产品手册',
        description: '产品使用说明文档',
        is_default: true,
        document_count: 1,
        created_at: '2026-09-01T00:00:00Z',
        updated_at: '2026-09-01T00:00:00Z'
      }
    ]
  }
}

export const MOCK_SESSIONS: MockSession[] = [
  {
    id: 's-1',
    title: 'RAG 原理讨论',
    created_at: '2026-09-05T10:00:00Z',
    updated_at: '2026-09-05T10:30:00Z',
    last_message: '检索增强生成的原理是什么？',
    message_count: 2
  },
  {
    id: 's-2',
    title: 'Milvus 调优',
    created_at: '2026-09-04T09:00:00Z',
    updated_at: '2026-09-04T09:20:00Z',
    last_message: 'HNSW 参数怎么设置？',
    message_count: 2
  }
]

/** 会话历史消息响应（ChatView.loadSession 的 SessionResponse）。 */
export const SESSION_DETAILS: Record<string, unknown> = {
  's-1': {
    title: 'RAG 原理讨论',
    created_at: '2026-09-05T10:00:00Z',
    updated_at: '2026-09-05T10:30:00Z',
    messages: [
      { id: 'm-1', role: 'user', content: '检索增强生成的原理是什么？' },
      {
        id: 'm-2',
        role: 'assistant',
        content: 'RAG 通过先检索后生成的方式回答问题。',
        source_metadata: [
          {
            source: 'doc-1',
            score: 0.9,
            filename: 'rag.md',
            document_id: 'doc-1',
            chunk_index: 0,
            total_chunks: 4,
            content: 'RAG 是检索增强生成技术'
          }
        ]
      }
    ]
  },
  's-2': {
    title: 'Milvus 调优',
    created_at: '2026-09-04T09:00:00Z',
    updated_at: '2026-09-04T09:20:00Z',
    messages: [
      { id: 'm-3', role: 'user', content: 'Milvus HNSW 参数怎么设置？' },
      { id: 'm-4', role: 'assistant', content: 'HNSW 的 M 建议设为 16，efConstruction 设为 200。' }
    ]
  }
}

/** 将 SSE 事件数组编码为 fetch 流式响应体（与 ChatView 的 \n\n 切分协议一致）。 */
export function sseBody(events: object[]): string {
  return events.map(e => `data:${JSON.stringify(e)}`).join('\n\n') + '\n\n'
}

/** 预置登录态并安装全部 API mock。注意 async：addInitScript 必须 await，
 * 否则与随后的 page.goto 存在竞态，登录态脚本可能未注册即开始导航。 */
export async function installMocks(page: Page, state: MockState = createMockState()): Promise<void> {
  // 登录态：路由守卫要求 token 或 api_key
  await page.addInitScript(() => {
    localStorage.setItem('token', 'e2e-token')
    localStorage.setItem('api_key', 'e2e-key')
  })

  // catch-all：未逐一 mock 的接口返回空对象，避免 axios 网络错误重试拖慢测试
  page.route(/\/api\//, route => route.fulfill({ status: 200, contentType: 'application/json', body: '{}' }))

  // 认证信息（App.vue fetchMe）
  page.route(/\/api\/auth\/me$/, route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ user_id: 'user-1', username: 'tester' }) })
  )

  // 会话列表：GET 返回数组；POST（新建会话）返回单会话
  page.route(/\/api\/sessions\/$/, route => {
    if (route.request().method() === 'POST') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ id: 's-new', title: '新会话' })
      })
    } else {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SESSIONS) })
    }
  })

  // 快捷问题
  page.route(/\/api\/sessions\/quick_questions$/, route =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ quick_questions: ['什么是RAG?', '如何上传文档?'] })
    })
  )

  // 单会话详情（历史消息）
  page.route(/\/api\/sessions\/s-\d+$/, route => {
    const id = route.request().url().match(/(s-\d+)$/)?.[1] ?? 's-1'
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SESSION_DETAILS[id])
    })
  })

  // 知识库列表 / 创建 / 删除（有状态）
  page.route(/\/api\/knowledge_bases\/(?!\w)/, route => {
    const method = route.request().method()
    if (method === 'POST') {
      const body = route.request().postDataJSON() as { name?: string; description?: string }
      const kb: MockKB = {
        id: `kb-${state.kbs.length + 1}-${Date.now()}`,
        name: body.name ?? '未命名知识库',
        description: body.description,
        document_count: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString()
      }
      state.kbs.push(kb)
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(kb) })
    } else {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: state.kbs })
      })
    }
  })
  page.route(/\/api\/knowledge_bases\/(?!recommend)[\w-]+$/, route => {
    const id = route.request().url().match(/knowledge_bases\/([\w-]+)$/)?.[1]
    if (route.request().method() === 'DELETE') {
      const idx = state.kbs.findIndex(kb => kb.id === id)
      if (idx !== -1) state.kbs.splice(idx, 1)
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    } else {
      const kb = state.kbs.find(k => k.id === id)
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(kb ?? {}) })
    }
  })
  page.route(/\/api\/knowledge_bases\/recommend$/, route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  // 文档列表与上传
  page.route(/\/api\/documents\//, route => {
    if (route.request().method() === 'POST' && route.request().url().includes('/upload')) {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ id: 'doc-new', filename: 'rag.md' })
      })
    } else if (route.request().method() === 'GET') {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [], total: 0 })
      })
    } else {
      route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    }
  })

  // 分类与标签
  page.route(/\/api\/categories\/$/, route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )
  page.route(/\/api\/tags\/$/, route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
  )

  // 输入联想（输入防抖触发）
  page.route(/\/api\/chat\/suggestions$/, route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ suggestions: [] }) })
  )

  // 聊天流式接口：SSE 事件流（reasoning → content → end + sources）
  page.route(/\/api\/chat\/stream$/, route =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: sseBody([
        {
          type: 'reasoning',
          id: 'r-1',
          step: 'kb_retrieve',
          status: 'done',
          title: '检索知识库',
          content: '命中 3 个片段',
          timestamp: 1,
          metadata: { sources_count: 3 }
        },
        { type: 'content', content: 'RAG 是' },
        { type: 'content', content: '检索增强生成技术' },
        {
          type: 'end',
          message_id: 'msg-1',
          sources: [
            {
              source: 'doc-1',
              score: 0.92,
              filename: 'rag.md',
              document_id: 'doc-1',
              chunk_index: 0,
              total_chunks: 4,
              source_type: 'kb',
              content: 'RAG 是检索增强生成技术'
            }
          ],
          reasoning: [
            {
              id: 'r-1',
              step: 'kb_retrieve',
              status: 'done',
              title: '检索知识库',
              content: '命中 3 个片段',
              timestamp: 1,
              metadata: { sources_count: 3 }
            }
          ]
        }
      ])
    })
  )

  // 通知 WS：静默接受连接
  page.routeWebSocket(/\/api\/ws\/notifications/, () => {})

  // 上传进度 WS：auth 帧 → auth_ok → 推送进度（auth_ok 是上传继续的前置条件）
  page.routeWebSocket(/\/api\/documents\/upload\/progress\/ws\//, ws => {
    ws.onMessage(message => {
      if (String(message).includes('"type":"auth"')) {
        ws.send(JSON.stringify({ type: 'auth_ok' }))
        setTimeout(() => ws.send(JSON.stringify({ progress: 30, message: '正在解析文档' })), 100)
        setTimeout(() => ws.send(JSON.stringify({ progress: 70, message: '正在向量化' })), 200)
        setTimeout(() => ws.send(JSON.stringify({ progress: 100, status: 'completed', message: '处理完成' })), 300)
      }
    })
  })
}
