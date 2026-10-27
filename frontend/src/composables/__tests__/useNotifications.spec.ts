/**
 * useWebSocketNotifications 单元测试（专项 D4 补测）。
 *
 * 以桩 WebSocket 驱动真实 composable 逻辑，覆盖：连接与首帧鉴权、
 * auth_ok/心跳、connected 频道同步、通知分发（单/多类型 + 异常隔离 +
 * 退订）、subscribe/unsubscribe 门控、1008 认证失败不重连、
 * 异常断开 3s 重连、主动断开不重连（D5 回归守卫）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, h, type Ref } from 'vue'

vi.mock('@/utils/auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/auth')>()),
  getToken: () => 'jwt-token-123'
}))

import { useWebSocketNotifications } from '@/composables/useNotifications'

const CONNECTING = 0
const OPEN = 1
const CLOSED = 3

class FakeWebSocket {
  static OPEN = OPEN
  static instances: FakeWebSocket[] = []
  url: string
  readyState = CONNECTING
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: ((ev: { code: number }) => void) | null = null
  onerror: ((ev: unknown) => void) | null = null
  sent: unknown[] = []
  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(JSON.parse(data))
  }
  close() {
    this.readyState = CLOSED
    this.onclose?.({ code: 1000 })
  }
  /** 测试辅助：完成连接握手。 */
  open() {
    this.readyState = OPEN
    this.onopen?.()
  }
  /** 测试辅助：投递一条服务端通知。 */
  emit(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) })
  }
}

/** 挂载宿主组件触发 onMounted/onUnmounted 生命周期，捕获 composable 返回值。 */
function mountNotifications() {
  // 返回值在 setup 内同步捕获；以完整类型声明避免测试中断言非空
  const captured = {} as ReturnType<typeof useWebSocketNotifications>
  const Comp = defineComponent({
    setup() {
      Object.assign(captured, useWebSocketNotifications())
      return () => h('div')
    }
  })
  const wrapper = mount(Comp)
  return { wrapper, captured }
}

const lastSocket = () => {
  const socket = FakeWebSocket.instances.at(-1)
  if (!socket) throw new Error('WebSocket 尚未创建')
  return socket
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.useFakeTimers()
  localStorage.clear()
  localStorage.setItem('api_key', 'key-abc')
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('连接与首帧鉴权', () => {
  it('挂载即连接：默认不传全局频道，open 后发送 auth 帧（token + api_key）', () => {
    mountNotifications()
    const ws = lastSocket()

    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(ws.url).toContain('/api/ws/notifications')
    expect(ws.url).not.toContain('channels=')

    ws.open()
    expect(ws.sent[0]).toEqual({ type: 'auth', token: 'jwt-token-123', api_key: 'key-abc' })
  })

  it('auth_ok 后 isConnected 置真并启动 30s 心跳', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()
    ws.emit({ type: 'auth_ok' })

    expect((captured.isConnected as Ref<boolean>).value).toBe(true)

    vi.advanceTimersByTime(30_000)
    expect(ws.sent.at(-1)).toEqual({ type: 'ping' })

    // 下一周期继续心跳
    vi.advanceTimersByTime(30_000)
    expect(ws.sent.filter(m => m === undefined || (m as { type: string }).type === 'ping')).toHaveLength(2)
  })

  it('connected 事件同步已订阅频道列表', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()
    ws.emit({ type: 'auth_ok' })
    ws.emit({ type: 'connected', channels: ['kb:1', 'doc:2'] })

    expect((captured.subscribedChannels as { value: string[] }).value).toEqual(['kb:1', 'doc:2'])
  })
})

describe('通知分发', () => {
  it('单类型与多类型处理器都收到匹配通知；未匹配的不触发', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()

    const single: unknown[] = []
    const multi: unknown[] = []
    captured.onNotification({ type: 'kb.created', handler: n => single.push(n) })
    captured.onNotification({ type: ['doc.updated', 'kb.deleted'], handler: n => multi.push(n) })

    const notification = { type: 'kb.created', channel: 'kb:*', data: { id: '1' }, timestamp: 't' }
    ws.emit(notification)
    expect(single).toHaveLength(1)
    expect(multi).toHaveLength(0)

    ws.emit({ type: 'doc.updated', channel: 'doc:*', data: {}, timestamp: 't' })
    expect(multi).toHaveLength(1)
  })

  it('处理器异常被隔离，不影响其他处理器', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()

    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    captured.onNotification({ type: 'boom', handler: () => { throw new Error('handler bug') } })
    const ok = vi.fn()
    captured.onNotification({ type: 'boom', handler: ok })

    ws.emit({ type: 'boom', channel: 'kb:*', data: {}, timestamp: 't' })
    expect(ok).toHaveBeenCalledTimes(1)
    expect(errSpy).toHaveBeenCalled()
  })

  it('退订函数移除处理器，之后不再收到通知', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()

    const handler = vi.fn()
    const off = captured.onNotification({ type: 'kb.created', handler })
    off()

    ws.emit({ type: 'kb.created', channel: 'kb:*', data: {}, timestamp: 't' })
    expect(handler).not.toHaveBeenCalled()
  })

  it('lastNotification 记录最近一条通知', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()
    const notification = { type: 'kb.created', channel: 'kb:*', data: { id: '9' }, timestamp: 't' }
    ws.emit(notification)

    expect((captured.lastNotification as { value: unknown }).value).toEqual(notification)
  })
})

describe('subscribe / unsubscribe 门控', () => {
  it('连接打开时发送对应帧；未打开时不发送', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()

    // 尚未 open（CONNECTING）：不发送
    captured.subscribe('kb:1')
    expect(ws.sent).toHaveLength(0)

    ws.open()
    captured.subscribe('kb:1')
    captured.subscribe(['doc:1', 'doc:2'])
    captured.unsubscribe('kb:1')

    expect(ws.sent.slice(-3)).toEqual([
      { type: 'subscribe', channels: ['kb:1'] },
      { type: 'subscribe', channels: ['doc:1', 'doc:2'] },
      { type: 'unsubscribe', channels: ['kb:1'] }
    ])
  })
})

describe('断开与重连策略', () => {
  it('1008 认证失败：标记失败并提示，不再重连', () => {
    const { captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()

    // 模拟服务端以 1008 关闭：readyState 先置 CLOSED 再触发 onclose
    ws.readyState = CLOSED
    ws.onclose?.({ code: 1008 })

    expect((captured.isConnected as Ref<boolean>).value).toBe(false)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1) // 无新连接
  })

  it('异常断开（非主动）：3 秒后自动重连出新连接', () => {
    mountNotifications()
    const ws = lastSocket()
    ws.open()

    ws.readyState = CLOSED // 同步桩状态（见 1008 用例注释）
    ws.onclose?.({ code: 1006 })
    vi.advanceTimersByTime(2_999)
    expect(FakeWebSocket.instances).toHaveLength(1)

    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
  })

  it('主动 disconnect（D5）：不触发自动重连', () => {
    const { wrapper, captured } = mountNotifications()
    const ws = lastSocket()
    ws.open()

    captured.disconnect()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)

    wrapper.unmount() // 卸载路径 disconnect 同样不重连
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
