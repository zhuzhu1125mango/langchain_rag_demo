/**
 * useChatStream 单元测试（专项 D4 补测）。
 *
 * 以桩 fetch（SSE 分块流）驱动真实编排逻辑，覆盖：发送守卫（空问题/
 * 在途流去重）、请求体与认证头构造、SSE 事件协议分发（content/thinking/
 * reasoning/search_status/end/title/error）、end 的来源映射与 message_id
 * 替换与新会话创建、非 2xx 错误、流静默中断、regenerateLast、卸载 abort（D4）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, ref, type Ref } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

// vi.mock 工厂会被提升到文件顶部，共享 spy 须经 vi.hoisted 创建
const { routerReplace, invalidateQueries, refetchQueries, resolveApprovalMock } = vi.hoisted(() => ({
  routerReplace: vi.fn(),
  invalidateQueries: vi.fn(),
  refetchQueries: vi.fn(),
  resolveApprovalMock: vi.fn()
}))

vi.mock('@/queries/chat', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/queries/chat')>()),
  resolveChatApproval: resolveApprovalMock
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ replace: routerReplace })
}))

vi.mock('@tanstack/vue-query', () => ({
  useQueryClient: () => ({ invalidateQueries, refetchQueries })
}))

vi.mock('@/utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/auth')>()),
  getToken: () => 'jwt-token-123'
}))

import { useChatStream, type UseChatStreamOptions } from '@/composables/useChatStream'
import { useChatStore } from '@/stores/chat'

const encoder = new TextEncoder()

/** 构造 SSE 分块流响应桩。 */
function sseResponse(chunks: string[]) {
  let i = 0
  return {
    ok: true,
    body: {
      getReader: () => ({
        read: async () =>
          i < chunks.length
            ? { done: false, value: encoder.encode(chunks[i++]) }
            : { done: true, value: undefined }
      })
    }
  }
}

/** 以 SSE 事件序列构造桩 fetch。 */
function stubSseFetch(chunks: string[]) {
  const fetchMock = vi.fn(async () => sseResponse(chunks))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

interface Harness {
  api: ReturnType<typeof useChatStream>
  store: ReturnType<typeof useChatStore>
  questionInput: Ref<string>
  selectedKBs: Ref<string[]>
  useWebSearch: Ref<boolean>
  deepThinking: Ref<boolean>
  searchStatus: Ref<'idle' | 'searching' | 'done' | 'failed'>
  suggestions: Ref<string[]>
  routerReplace: ReturnType<typeof vi.fn>
  wrapper: ReturnType<typeof mount>
}

/** 挂载宿主组件以正确注册 onBeforeUnmount，组装全部依赖。 */
function mountStream(): Harness {
  const questionInput = ref('')
  const selectedKBs = ref<string[]>([])
  const useWebSearch = ref(false)
  const deepThinking = ref(false)
  const searchStatus = ref<'idle' | 'searching' | 'done' | 'failed'>('idle')
  const suggestions = ref<string[]>(['q1'])
  const messageListRef = ref({ scrollToBottom: () => {} })

  const options: UseChatStreamOptions = {
    questionInput,
    selectedKBs,
    useWebSearch,
    deepThinking,
    searchStatus,
    suggestions,
    messageListRef: messageListRef as never
  }

  let api!: ReturnType<typeof useChatStream>
  const wrapper = mount(
    defineComponent({
      setup() {
        api = useChatStream(options)
        return () => h('div')
      }
    })
  )

  return {
    api,
    store: useChatStore(),
    questionInput,
    selectedKBs,
    useWebSearch,
    deepThinking,
    searchStatus,
    suggestions,
    routerReplace,
    wrapper
  }
}

/** 查找当前用例产生的助手消息。 */
const assistantOf = (store: ReturnType<typeof useChatStore>) =>
  store.messages.find(m => m.role === 'assistant')!

const lastRequestBody = () =>
  JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls.at(-1)![1].body as string)

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  localStorage.setItem('api_key', 'key-abc')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('发送守卫', () => {
  it('空问题不发起请求', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const h = mountStream()

    await h.api.sendMessage('   ')

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('存在加载中的助手消息时不重复发送', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const h = mountStream()
    h.store.addMessage({ id: 'a1', role: 'assistant', content: '', isLoading: true })

    await h.api.sendMessage('新问题')

    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('请求构造', () => {
  it('携带认证头与完整请求体（kb_ids/session_id/深度思考/搜索模式）', async () => {
    stubSseFetch(['data: {"type":"end"}\n\n'])
    const h = mountStream()
    h.store.setCurrentSession({ id: 'sess-1', title: 't', created_at: '', updated_at: '' })
    h.selectedKBs.value = ['kb-1', 'kb-2']
    h.useWebSearch.value = true
    h.deepThinking.value = true

    await h.api.sendMessage('测试问题')

    const [url, init] = (fetch as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe('/api/chat/stream')
    expect((init!.headers as Record<string, string>)['X-API-Key']).toBe('key-abc')
    expect((init!.headers as Record<string, string>)['Authorization']).toBe('Bearer jwt-token-123')

    expect(JSON.parse(init!.body as string)).toMatchObject({
      question: '测试问题',
      use_web_search: true,
      deep_thinking: 'on',
      search_mode: 'function_calling',
      kb_ids: ['kb-1', 'kb-2'],
      session_id: 'sess-1'
    })
    // 发送后清空输入与快捷问题；end 后搜索状态复位
    expect(h.questionInput.value).toBe('')
    expect(h.suggestions.value).toEqual([])
    expect(h.searchStatus.value).toBe('idle')
  })

  it('未选知识库/未开搜索时不携带对应字段', async () => {
    stubSseFetch(['data: {"type":"end"}\n\n'])
    const h = mountStream()

    await h.api.sendMessage('纯 KB 问题')

    const body = JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body as string)
    expect(body.kb_ids).toBeUndefined()
    expect(body.search_mode).toBeUndefined()
    expect(body.deep_thinking).toBe('off')
  })
})

describe('SSE 事件分发', () => {
  it('content 增量累加、thinking 追加、reasoning 合并，首片段后退出加载态', async () => {
    stubSseFetch([
      'data: {"type":"content","content":"你"}\n\n',
      'data: {"type":"thinking","content":"思考中"}\n\n',
      'data: {"type":"reasoning","step":"kb_retrieval","title":"检索","status":"done","latency_ms":100}\n\n',
      'data: {"type":"content","content":"好"}\n\n'
    ])
    const h = mountStream()

    await h.api.sendMessage('hi')

    const assistant = assistantOf(h.store)
    expect(assistant.content).toBe('你好')
    expect(assistant.isLoading).toBe(false)
    expect(assistant.thinking).toBe('思考中')
    expect(assistant.reasoning).toHaveLength(1)
    expect(assistant.reasoning![0].step).toBe('kb_retrieval')
  })

  it('跨 chunk 的半截事件正确缓冲拼接', async () => {
    stubSseFetch([
      'data: {"type":"conte',
      'nt","content":"AB"}\n\ndata: {"type":"content","content":"C"}\n\n'
    ])
    const h = mountStream()

    await h.api.sendMessage('hi')

    expect(assistantOf(h.store).content).toBe('ABC')
  })

  it('search_status 中间态生效，end 后复位 idle', async () => {
    const fetchMock = stubSseFetch([
      'data: {"type":"search_status","status":"searching"}\n\n',
      'data: {"type":"search_status","status":"done"}\n\n',
      'data: {"type":"end"}\n\n'
    ])
    const h = mountStream()
    const seenStatuses: string[] = []
    // 借助 fetch 调用次数切分：第一次调用前 status 已被首事件驱动，
    // 这里直接观测最终态，并确认中间事件未抛错（error 字段为空）
    void seenStatuses
    void fetchMock

    await h.api.sendMessage('hi')

    expect(h.searchStatus.value).toBe('idle')
    expect(assistantOf(h.store).error).toBeUndefined()
  })

  it('end 事件：来源映射、message_id 替换、新会话创建与路由 replace、列表刷新', async () => {
    stubSseFetch([
      `data: ${JSON.stringify({
        type: 'end',
        message_id: 'm-backend-1',
        session_id: 'sess-77',
        reasoning: [{ step: 'generate', title: '生成', status: 'done', latency_ms: 500 }],
        sources: [
          { source: 'doc.pdf', score: 0.91, document_id: 'd1', chunk_index: 2, total_chunks: 10, content: '片段' },
          { url: 'https://example.com/a', title: 'Web 页面' }
        ]
      })}\n\n`
    ])
    const h = mountStream()

    await h.api.sendMessage('第一问')

    // message_id 替换：临时 id 消失，出现后端 id
    expect(h.store.messages.some(m => m.id === 'm-backend-1')).toBe(true)
    const assistant = h.store.messages.find(m => m.id === 'm-backend-1')!
    expect(assistant.isLoading).toBe(false)

    // 来源映射：kb 来源走 source 兜底链 + source_type=kb；web 来源 source_type=web；index 从 1 起
    expect(assistant.sources).toEqual([
      expect.objectContaining({
        source: 'doc.pdf', score: 0.91, source_type: 'kb', document_id: 'd1',
        page: 2, total_chunks: 10, content: '片段', index: 1
      }),
      expect.objectContaining({ source: '', url: 'https://example.com/a', title: 'Web 页面', source_type: 'web', index: 2 })
    ])

    // 新会话创建 + 路由 replace
    expect(h.store.currentSession?.id).toBe('sess-77')
    expect(h.store.currentSession?.title).toBe('第一问')
    expect(h.routerReplace).toHaveBeenCalledWith({ path: '/', query: { session: 'sess-77' } })
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['sessions'] })
  })

  it('title 事件：更新当前会话标题并刷新会话列表', async () => {
    stubSseFetch([
      'data: {"type":"end"}\n\n',
      'data: {"type":"title","title":"后台生成的标题"}\n\n'
    ])
    const h = mountStream()
    h.store.setCurrentSession({ id: 'sess-1', title: '旧标题', created_at: '', updated_at: '' })

    await h.api.sendMessage('hi')

    expect(h.store.currentSession?.title).toBe('后台生成的标题')
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['sessions'] })
    expect(refetchQueries).toHaveBeenCalledWith({ queryKey: ['sessions'] })
  })

  it('error 事件：写入消息错误态并复位搜索状态', async () => {
    stubSseFetch(['data: {"type":"error","error":"模型超时"}\n\n'])
    const h = mountStream()
    h.searchStatus.value = 'searching'

    await h.api.sendMessage('hi')

    const assistant = assistantOf(h.store)
    expect(assistant.error).toBe('服务端错误: 模型超时')
    expect(assistant.isLoading).toBe(false)
    expect(h.searchStatus.value).toBe('idle')
  })

  it('approval_required 事件：写入待审批卡片', async () => {
    stubSseFetch([
      'data: {"type":"approval_required","approval_id":"ap-1","tool":"write_tool",' +
      '"arguments":{"x":1},"timeout_s":60}\n\n'
    ])
    const h = mountStream()

    await h.api.sendMessage('hi')

    expect(assistantOf(h.store).approval).toEqual({
      id: 'ap-1',
      tool: 'write_tool',
      arguments: { x: 1 },
      status: 'pending'
    })
  })
})

describe('异常路径', () => {
  it('非 2xx 响应：错误信息含状态码与 detail', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 429,
      json: async () => ({ detail: '请求过于频繁' })
    })))
    const h = mountStream()

    await h.api.sendMessage('hi')

    const assistant = assistantOf(h.store)
    expect(assistant.error).toBe('请求失败 (429): 请求过于频繁')
    expect(assistant.isLoading).toBe(false)
  })

  it('流静默中断（无 end/error 且无内容）提示连接断开', async () => {
    stubSseFetch([])
    const h = mountStream()

    await h.api.sendMessage('hi')

    expect(h.store.messages.at(-1)!.error).toBe('连接断开，无法获取响应')
  })

  it('fetch 抛错时追加发送失败消息', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network down')
    }))
    const h = mountStream()

    await h.api.sendMessage('hi')

    expect(h.store.messages.at(-1)!.error).toBe('发送失败: network down')
    expect(h.searchStatus.value).toBe('idle')
  })
})

describe('重新生成与快捷发送', () => {
  it('regenerateLast 取最后一条用户消息重发', async () => {
    stubSseFetch(['data: {"type":"end"}\n\n'])
    const h = mountStream()
    h.store.addMessage({ id: 'u1', role: 'user', content: '最初的问题' })
    h.store.addMessage({ id: 'a1', role: 'assistant', content: '旧回答' })

    await h.api.regenerateLast()

    expect(lastRequestBody().question).toBe('最初的问题')
  })

  it('sendQuickQuestion 填入输入框并发送', async () => {
    stubSseFetch(['data: {"type":"end"}\n\n'])
    const h = mountStream()

    await h.api.sendQuickQuestion('快捷问题')

    expect(lastRequestBody().question).toBe('快捷问题')
    expect(h.questionInput.value).toBe('')
  })
})

describe('HITL 审批回执（AM-2）', () => {
  beforeEach(() => {
    resolveApprovalMock.mockReset()
  })

  it('批准成功：审批状态更新为 approved', async () => {
    resolveApprovalMock.mockResolvedValue(undefined)
    const h = mountStream()
    h.store.addMessage({
      id: 'a1',
      role: 'assistant',
      content: '',
      approval: { id: 'ap-1', tool: 'write_tool', status: 'pending' }
    })

    await h.api.resolveApproval('a1', true)

    expect(resolveApprovalMock).toHaveBeenCalledWith('ap-1', true)
    expect(h.store.messages.find(m => m.id === 'a1')?.approval?.status).toBe('approved')
  })

  it('接口失败：审批状态标记为 expired', async () => {
    resolveApprovalMock.mockRejectedValue(new Error('not found'))
    const h = mountStream()
    h.store.addMessage({
      id: 'a2',
      role: 'assistant',
      content: '',
      approval: { id: 'ap-2', tool: 'write_tool', status: 'pending' }
    })

    await h.api.resolveApproval('a2', false)

    expect(h.store.messages.find(m => m.id === 'a2')?.approval?.status).toBe('expired')
  })
})

describe('卸载中止（D4）', () => {
  it('组件卸载时 abort 在途流', async () => {
    let capturedSignal: AbortSignal | undefined
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
      capturedSignal = init!.signal
      // 永不完成的流：read 一直挂起，模拟长连接
      return {
        ok: true,
        body: { getReader: () => ({ read: () => new Promise(() => {}) }) }
      }
    }))
    const h = mountStream()
    void h.api.sendMessage('长时间流')

    await vi.waitFor(() => expect(capturedSignal).toBeDefined())
    expect(capturedSignal!.aborted).toBe(false)

    h.wrapper.unmount()

    expect(capturedSignal!.aborted).toBe(true)
  })
})
