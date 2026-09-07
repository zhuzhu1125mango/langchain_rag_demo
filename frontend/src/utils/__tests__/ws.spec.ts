import { describe, it, expect, vi, afterEach } from 'vitest'
import { buildWsUrl } from '../ws'

describe('buildWsUrl', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('开发环境走 localhost:8000', () => {
    // vitest 下 import.meta.env.DEV 默认为 true
    expect(buildWsUrl('/api/chat/ws')).toBe('ws://localhost:8000/api/chat/ws')
  })

  it('生产环境 http 走当前 host 的 ws 协议', () => {
    vi.stubEnv('DEV', false)
    Object.defineProperty(window, 'location', {
      value: new URL('http://example.com:8080'),
      writable: true
    })
    expect(buildWsUrl('/api/chat/ws')).toBe('ws://example.com:8080/api/chat/ws')
  })

  it('生产环境 https 升级为 wss 协议', () => {
    vi.stubEnv('DEV', false)
    Object.defineProperty(window, 'location', {
      value: new URL('https://example.com'),
      writable: true
    })
    expect(buildWsUrl('/api/chat/ws')).toBe('wss://example.com/api/chat/ws')
  })
})
