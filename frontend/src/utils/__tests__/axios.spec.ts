/**
 * utils/axios 重试策略单元测试（HG 残项：axios 5xx 非幂等重试）。
 *
 * 覆盖分流边界：
 * - 连接被拒绝（ECONNREFUSED）：请求未到达服务端 → 任何方法可重试；
 * - 超时 / 5xx：服务端可能已处理 → 仅幂等方法（GET/HEAD/OPTIONS/PUT/DELETE）可重试，
 *   POST/PATCH 不得重试（避免重复建会话、重复删除、重复扣费）；
 * - 4xx 与达到重试上限：一律不重试。
 */
import { describe, it, expect } from 'vitest'
import { isRetryableRequest } from '../axios'

const IDEMPOTENT = ['get', 'head', 'options', 'put', 'delete']
const NON_IDEMPOTENT = ['post', 'patch']

describe('isRetryableRequest', () => {
  it('连接被拒绝时任何方法都可重试', () => {
    for (const method of [...IDEMPOTENT, ...NON_IDEMPOTENT]) {
      expect(isRetryableRequest({ method, code: 'ECONNREFUSED', retryCount: 0 })).toBe(true)
    }
  })

  it('5xx 仅幂等方法可重试', () => {
    for (const method of IDEMPOTENT) {
      expect(isRetryableRequest({ method, status: 503, retryCount: 0 })).toBe(true)
    }
    for (const method of NON_IDEMPOTENT) {
      expect(isRetryableRequest({ method, status: 503, retryCount: 0 })).toBe(false)
    }
  })

  it('超时仅幂等方法可重试', () => {
    expect(isRetryableRequest({ method: 'get', code: 'ETIMEDOUT', retryCount: 0 })).toBe(true)
    expect(isRetryableRequest({ method: 'post', code: 'ETIMEDOUT', retryCount: 0 })).toBe(false)
  })

  it('方法名大小写与缺省方法归一化', () => {
    expect(isRetryableRequest({ method: 'GET', status: 500, retryCount: 0 })).toBe(true)
    expect(isRetryableRequest({ method: 'Post', status: 500, retryCount: 0 })).toBe(false)
    // axios 未显式设置 method 时按 GET 处理
    expect(isRetryableRequest({ status: 500, retryCount: 0 })).toBe(true)
  })

  it('4xx 与业务错误不重试', () => {
    expect(isRetryableRequest({ method: 'get', status: 400, retryCount: 0 })).toBe(false)
    expect(isRetryableRequest({ method: 'get', status: 404, retryCount: 0 })).toBe(false)
    expect(isRetryableRequest({ method: 'get', status: 429, retryCount: 0 })).toBe(false)
    expect(isRetryableRequest({ method: 'get', code: 'ERR_BAD_REQUEST', retryCount: 0 })).toBe(false)
  })

  it('达到重试上限后不再重试', () => {
    expect(isRetryableRequest({ method: 'get', status: 500, retryCount: 2 })).toBe(true)
    expect(isRetryableRequest({ method: 'get', status: 500, retryCount: 3 })).toBe(false)
    expect(isRetryableRequest({ method: 'get', code: 'ECONNREFUSED', retryCount: 3 })).toBe(false)
  })
})
