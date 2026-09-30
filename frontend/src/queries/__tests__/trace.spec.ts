/**
 * queries/trace.ts 单元测试（专项 D4 补测）。
 *
 * 覆盖：STAGE_LABELS 契约、useTraces 请求参数与响应透传、params 响应式
 * 触发重新请求（W6 #33 回归守卫）、useTraceDetail 的 enabled 门控与详情请求。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h, ref, toValue } from 'vue'
import { QueryClient, VueQueryPlugin } from '@tanstack/vue-query'

vi.mock('@/utils/axios', () => ({
  api: { get: vi.fn() }
}))

import { api } from '@/utils/axios'
import { useTraces, useTraceDetail, STAGE_LABELS } from '@/queries/trace'

const mockedGet = vi.mocked(api.get)

/** 挂载一个调用 composable 的宿主组件，捕获返回值。 */
function mountWithQuery<R>(setup: () => R) {
  const captured: { result?: R } = {}
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  })
  const Comp = defineComponent({
    setup() {
      captured.result = setup()
      return () => h('div')
    }
  })
  mount(Comp, {
    global: { plugins: [[VueQueryPlugin, { queryClient: client }]] }
  })
  return captured
}

/** 解包可能是 Ref 的值。 */
const unwrap = <T>(value: unknown): T => toValue(value as never) as T

const listFixture = {
  total: 2,
  items: [
    {
      id: 't1',
      question: '什么是RAG',
      fallback_triggered: false,
      stage_count: 3,
      total_latency_ms: 1200
    }
  ]
}

beforeEach(() => {
  mockedGet.mockReset()
})

describe('STAGE_LABELS 契约', () => {
  it('覆盖后端全部 9 个阶段标识且为非空中文标签', () => {
    const expected = [
      'context_enhance', 'datetime_tool', 'intent_route', 'tool_first',
      'kb_decision', 'agent_mode', 'web_search', 'kb_retrieval', 'generate'
    ]
    expect(Object.keys(STAGE_LABELS).sort()).toEqual([...expected].sort())
    for (const label of Object.values(STAGE_LABELS)) {
      expect(label.length).toBeGreaterThan(0)
    }
  })
})

describe('useTraces', () => {
  it('以 /traces 与过滤参数发请求并透传响应', async () => {
    mockedGet.mockResolvedValue(listFixture)

    const { result } = mountWithQuery(() => useTraces(() => ({ session_id: 's1', limit: 10 })))
    await flushPromises()

    expect(mockedGet).toHaveBeenCalledWith('/traces', { params: { session_id: 's1', limit: 10 } })
    expect(unwrap(result!.data)).toEqual(listFixture)
  })

  it('params 变化时以新参数重新请求（queryKey 响应性回归守卫）', async () => {
    mockedGet.mockResolvedValue(listFixture)

    const params = ref<{ session_id?: string }>({ session_id: 's1' })
    const { result } = mountWithQuery(() => useTraces(params))
    await flushPromises()
    expect(mockedGet).toHaveBeenCalledTimes(1)

    params.value = { session_id: 's2' }
    await flushPromises()

    expect(mockedGet).toHaveBeenCalledTimes(2)
    expect(mockedGet).toHaveBeenLastCalledWith('/traces', { params: { session_id: 's2' } })
    // 请求期间保留上一份数据（placeholderData）
    expect(unwrap(result!.data)).toEqual(listFixture)
  })
})

describe('useTraceDetail', () => {
  it('id 为空时不发请求（enabled 门控）', async () => {
    const { result } = mountWithQuery(() => useTraceDetail(() => null))
    await flushPromises()

    expect(mockedGet).not.toHaveBeenCalled()
    expect(unwrap(result!.fetchStatus)).toBe('idle')
  })

  it('id 就绪后请求 /traces/{id} 并透传详情', async () => {
    const detail = { ...listFixture.items[0], stages: [{ name: 'generate', status: 'done', latency_ms: 800 }] }
    mockedGet.mockResolvedValue(detail)

    const id = ref<string | null>(null)
    const captured = mountWithQuery(() => useTraceDetail(id))
    await flushPromises()
    expect(mockedGet).not.toHaveBeenCalled()

    id.value = 'abc-1'
    await flushPromises()

    expect(mockedGet).toHaveBeenCalledWith('/traces/abc-1')
    expect(unwrap(captured.result!.data)).toEqual(detail)
  })
})
