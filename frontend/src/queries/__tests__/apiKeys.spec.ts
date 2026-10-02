import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildExpiresAt,
  createApiKey,
  deriveKeyStatus,
  fetchApiKeys,
  fetchApiKeysConfigInfo,
  formatRelativeTime,
  revokeApiKey,
  type UserApiKey
} from '../apiKeys'
import { api } from '@/utils/axios'

/**
 * 用户级 API Key queries 层测试（E.6）。
 *
 * 覆盖：API 调用封装（GET/POST/DELETE 路径与载荷）、状态三态推导、
 * 有效期换算、相对时间格式化。Vue Query hooks 依赖组件上下文，
 * 此处直接测其包装的纯函数。
 */
vi.mock('@/utils/axios', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn()
  }
}))

const mockedApi = vi.mocked(api)

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

function makeKey(overrides: Partial<UserApiKey> = {}): UserApiKey {
  return {
    id: 'k-1',
    name: 'claude-code',
    key_prefix: 'rag_a1b2c3d4',
    created_at: '2026-09-01T00:00:00Z',
    last_used_at: null,
    expires_at: null,
    revoked_at: null,
    ...overrides
  }
}

describe('API 调用封装', () => {
  it('fetchApiKeys 调 GET /auth/api-keys 并返回列表', async () => {
    const list = [makeKey()]
    mockedApi.get.mockResolvedValueOnce(list)
    await expect(fetchApiKeys()).resolves.toEqual(list)
    expect(mockedApi.get).toHaveBeenCalledWith('/auth/api-keys')
  })

  it('createApiKey 调 POST /auth/api-keys 并透传载荷（响应含一次性明文）', async () => {
    const created = { id: 'k-1', name: 'ci', key_prefix: 'rag_x', key: 'rag_secret', created_at: 't' }
    mockedApi.post.mockResolvedValueOnce(created)
    await expect(createApiKey({ name: 'ci', expires_in_days: 30 })).resolves.toEqual(created)
    expect(mockedApi.post).toHaveBeenCalledWith('/auth/api-keys', { name: 'ci', expires_in_days: 30 })
  })

  it('revokeApiKey 调 DELETE /auth/api-keys/:id', async () => {
    mockedApi.delete.mockResolvedValueOnce(undefined)
    await expect(revokeApiKey('k-1')).resolves.toBeUndefined()
    expect(mockedApi.delete).toHaveBeenCalledWith('/auth/api-keys/k-1')
  })

  it('fetchApiKeysConfigInfo 调 GET /config/info', async () => {
    mockedApi.get.mockResolvedValueOnce({ user_api_keys_enabled: true, user_api_keys_max_per_user: 5 })
    await expect(fetchApiKeysConfigInfo()).resolves.toEqual({
      user_api_keys_enabled: true,
      user_api_keys_max_per_user: 5
    })
    expect(mockedApi.get).toHaveBeenCalledWith('/config/info')
  })
})

describe('deriveKeyStatus 三态推导', () => {
  const now = new Date('2026-09-30T12:00:00Z')

  it('已吊销优先于有效期判断', () => {
    const key = makeKey({ revoked_at: '2026-09-15T00:00:00Z', expires_at: '2026-09-01T00:00:00Z' })
    expect(deriveKeyStatus(key, now)).toBe('revoked')
  })

  it('过期时间早于当前时间为 expired', () => {
    expect(deriveKeyStatus(makeKey({ expires_at: '2026-09-30T11:59:59Z' }), now)).toBe('expired')
  })

  it('未吊销未过期（含永久）为 active', () => {
    expect(deriveKeyStatus(makeKey(), now)).toBe('active')
    expect(deriveKeyStatus(makeKey({ expires_at: '2026-10-30T00:00:00Z' }), now)).toBe('active')
  })
})

describe('buildExpiresAt 与 formatRelativeTime', () => {
  it('buildExpiresAt：天数换算为未来 ISO 时间，0/undefined 为 null（永久）', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-30T00:00:00Z'))
    expect(buildExpiresAt(undefined)).toBeNull()
    expect(buildExpiresAt(0)).toBeNull()
    expect(new Date(buildExpiresAt(30) as string).getTime()).toBe(
      new Date('2026-10-30T00:00:00Z').getTime()
    )
  })

  it('formatRelativeTime：空值/无效值为占位符', () => {
    expect(formatRelativeTime(null)).toBe('—')
    expect(formatRelativeTime('not-a-date')).toBe('—')
  })

  it('formatRelativeTime：分钟/小时/天/超 30 天回退日期', () => {
    const now = new Date('2026-09-30T12:00:00Z')
    expect(formatRelativeTime('2026-09-30T11:59:40Z', now)).toBe('刚刚')
    expect(formatRelativeTime('2026-09-30T11:30:00Z', now)).toBe('30 分钟前')
    expect(formatRelativeTime('2026-09-30T09:00:00Z', now)).toBe('3 小时前')
    expect(formatRelativeTime('2026-09-28T12:00:00Z', now)).toBe('2 天前')
    expect(formatRelativeTime('2026-08-01T00:00:00Z', now)).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })
})
