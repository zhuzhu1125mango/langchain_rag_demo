import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildSessionsPage,
  fetchAllSessionIds,
  SESSIONS_MAX_LIMIT,
  SESSIONS_PAGE_SIZE
} from '../chat'
import { api } from '@/utils/axios'

/**
 * 会话列表分页（P0-1）的前端逻辑测试。
 *
 * 这里的错误都不会抛异常，只会「看起来正常、数据不对」：
 * - `hasMore` 判反 → 「加载更多」按钮该在时不在，第 51 条起永远看不到；
 * - `nextOffset` 算错 → 一直请求同一页，或者整页数据被跳过；
 * - 批量删除只作用于已加载页 → 提示删了 N 条，刷新又回来。
 */

vi.mock('@/utils/axios', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
}))

const mockedApi = vi.mocked(api)

/** 造一页会话，id 用 `s-{n}` 便于断言顺序。 */
function makeSessions(count: number, prefix = 's'): { id: string; title: string; created_at: string; updated_at: string }[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `${prefix}-${i}`,
    title: `会话 ${i}`,
    created_at: '2026-11-01T00:00:00+00:00',
    updated_at: '2026-11-01T00:00:00+00:00'
  }))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('buildSessionsPage', () => {
  it('装满一页时认为还有下一页，偏移等于当前偏移加本页条数', () => {
    const page = buildSessionsPage(makeSessions(SESSIONS_PAGE_SIZE), 0)
    expect(page.hasMore).toBe(true)
    expect(page.nextOffset).toBe(SESSIONS_PAGE_SIZE)
  })

  it('不满一页时认为到底了（不会多发一次空请求）', () => {
    const page = buildSessionsPage(makeSessions(3), 100)
    expect(page.hasMore).toBe(false)
    expect(page.nextOffset).toBe(103)
  })

  it('空页不会退化成「还有更多」', () => {
    expect(buildSessionsPage([], 0).hasMore).toBe(false)
  })
})

describe('fetchAllSessionIds', () => {
  it('翻完所有页，而不是只取第一页（批量删除的作用域）', async () => {
    mockedApi.get = vi
      .fn()
      .mockResolvedValueOnce(makeSessions(SESSIONS_MAX_LIMIT))
      .mockResolvedValueOnce(makeSessions(3, 'tail'))

    const ids = await fetchAllSessionIds()

    expect(ids).toHaveLength(SESSIONS_MAX_LIMIT + 3)
    expect(mockedApi.get).toHaveBeenCalledTimes(2)
    // 第二页的偏移必须接在首页之后，否则会重复取第一页、漏掉真正的尾巴
    expect(mockedApi.get).toHaveBeenLastCalledWith('/sessions/', {
      params: { skip: SESSIONS_MAX_LIMIT, limit: SESSIONS_MAX_LIMIT }
    })
  })

  it('结果不足一页时只请求一次', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(makeSessions(2))

    const ids = await fetchAllSessionIds()

    expect(ids).toEqual(['s-0', 's-1'])
    expect(mockedApi.get).toHaveBeenCalledTimes(1)
  })

  it('带上筛选条件：删除筛选结果时不能把范围放大到全部历史', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(makeSessions(1))

    await fetchAllSessionIds({ q: '休假', kb_id: 'kb-1', date_from: '2026-11-01' })

    expect(mockedApi.get).toHaveBeenCalledWith('/sessions/', {
      params: {
        q: '休假',
        kb_id: 'kb-1',
        date_from: '2026-11-01',
        skip: 0,
        limit: SESSIONS_MAX_LIMIT
      }
    })
  })

  it('空筛选值不会写成空参数（`q=` 会让 URL 与缓存键变脏）', async () => {
    mockedApi.get = vi.fn().mockResolvedValue([])

    await fetchAllSessionIds({ q: '   ', kb_id: '' })

    expect(mockedApi.get).toHaveBeenCalledWith('/sessions/', {
      params: { skip: 0, limit: SESSIONS_MAX_LIMIT }
    })
  })

  it('后端一直返回满页时按 maxPages 停下，不会死循环', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(makeSessions(SESSIONS_MAX_LIMIT))

    const ids = await fetchAllSessionIds(undefined, 2)

    expect(mockedApi.get).toHaveBeenCalledTimes(2)
    expect(ids).toHaveLength(SESSIONS_MAX_LIMIT * 2)
  })

  it('请求失败时把异常抛给调用方（页面据此提示且不删除任何东西）', async () => {
    mockedApi.get = vi.fn().mockRejectedValue(new Error('boom'))

    await expect(fetchAllSessionIds()).rejects.toThrow('boom')
  })
})
