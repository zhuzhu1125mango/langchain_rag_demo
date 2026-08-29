/**
 * WebSocket通知Composable
 *
 * 提供实时通知功能，用于：
 * - 知识库列表变更
 * - 文档列表变更
 * - 任务进度更新
 */

import { ref, onMounted, onUnmounted } from 'vue'
import { useToast } from './useToast'
import { buildWsUrl } from '@/utils/ws'

export interface Notification {
  /** 通知类型，如 connected、kb.created 等。 */
  type: string
  /** 来源频道（单个通知的归属频道）。 */
  channel: string
  /** 当前已订阅的频道列表（仅 connected 通知返回）。 */
  channels?: string[]
  /** 通知业务数据载荷。 */
  data: Record<string, unknown>
  /** 时间戳（ISO 字符串）。 */
  timestamp: string
}

export interface NotificationHandler {
  /** 关注的通知类型，支持单个或多个。 */
  type: string | string[]
  /** 处理函数，接收匹配到的通知。 */
  handler: (notification: Notification) => void
}

/**
 * WebSocket 通知 Composable 入口。
 *
 * 组件挂载时自动连接通知服务，卸载时断开；维护连接状态、最近一条通知
 * 与已订阅频道，并提供 onNotification/subscribe/unsubscribe 能力。
 */
export function useWebSocketNotifications() {
  const toast = useToast()
  let ws: WebSocket | null = null
  const isConnected = ref(false)
  const lastNotification = ref<Notification | null>(null)
  const handlers: NotificationHandler[] = []
  let reconnectTimer: number | null = null
  let heartbeatTimer: number | null = null
  const subscribedChannels = ref<string[]>([])
  let authFailed = false

  // 默认订阅频道
  const defaultChannels = ['kb:*', 'doc:*']

  /** 建立连接：已连接则跳过，连接成功后启动心跳，断开后自动重连。 */
  function connect() {
    if (ws?.readyState === WebSocket.OPEN) {
      return
    }

    // 认证失败后不再尝试重连，避免无效重试风暴
    if (authFailed) {
      return
    }

    const channelParam = subscribedChannels.value.join(',') || defaultChannels.join(',')
    const apiKey = localStorage.getItem('api_key') || import.meta.env.VITE_API_KEY
    const queryParams = new URLSearchParams({ channels: channelParam })
    const wsUrl = buildWsUrl(`/api/ws/notifications?${queryParams.toString()}`)

    try {
      ws = new WebSocket(wsUrl)

      ws.onopen = () => {
        console.log('[WebSocket] Connected, sending auth frame')
        // 首帧鉴权：api_key 不再走 URL query（避免进反向代理访问日志），
        // 连接建立后立即发送 auth 帧；开发模式（后端未配置 API_KEY）服务端
        // 会不经校验直接回发 auth_ok。
        ws?.send(JSON.stringify({ type: 'auth', api_key: apiKey || '' }))
      }

      ws.onmessage = (event) => {
        try {
          const notification: Notification = JSON.parse(event.data)
          lastNotification.value = notification

          // 首帧鉴权结果：通过后启动心跳；未通过场景由服务端以 1008 关闭
          if (notification.type === 'auth_ok') {
            isConnected.value = true
            console.log('[WebSocket] Authenticated')
            startHeartbeat()
            return
          }

          if (notification.type === 'connected') {
            console.log('[WebSocket] Subscribed to channels:', notification.channels)
            subscribedChannels.value = notification.channels || []
          } else {
            handleNotification(notification)
          }
        } catch (e) {
          console.error('[WebSocket] Failed to parse notification:', e)
        }
      }

      ws.onclose = (event) => {
        isConnected.value = false
        console.log('[WebSocket] Disconnected')
        stopHeartbeat()

        // 认证失败（1008 Policy Violation）时标记失败并提示，不再重连
        if (event.code === 1008) {
          authFailed = true
          toast.error('WebSocket 认证失败，请检查 API Key 配置')
          return
        }

        scheduleReconnect()
      }

      ws.onerror = (error) => {
        console.error('[WebSocket] Error:', error)
      }
    } catch (e) {
      console.error('[WebSocket] Connection failed:', e)
      scheduleReconnect()
    }
  }

  /** 主动断开：清理重连与心跳定时器并关闭连接。 */
  function disconnect() {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    stopHeartbeat()
    if (ws) {
      ws.close()
      ws = null
    }
  }

  /**
   * 安排重连：采用固定 3 秒间隔的策略。
   *
   * 不做指数退避，避免长时间无连接；若已有待重连任务则直接返回，防止叠加。
   */
  function scheduleReconnect() {
    if (reconnectTimer) return
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 3000)
  }

  /**
   * 启动心跳：每 30 秒发送一次 ping，保持长连接活跃。
   *
   * 启动前先清理旧定时器，避免重复；仅当连接处于 OPEN 状态时才发送。
   */
  function startHeartbeat() {
    stopHeartbeat()
    heartbeatTimer = window.setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }))
      }
    }, 30000)
  }

  /** 停止心跳定时器。 */
  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer)
      heartbeatTimer = null
    }
  }

  /** 将通知分发给匹配类型的处理器，单个处理器异常不影响其他处理器。 */
  function handleNotification(notification: Notification) {
    // 遍历所有处理器
    for (const handler of handlers) {
      const types = Array.isArray(handler.type) ? handler.type : [handler.type]
      if (types.includes(notification.type)) {
        try {
          handler.handler(notification)
        } catch (e) {
          console.error('[WebSocket] Handler error:', e)
        }
      }
    }
  }

  /** 注册通知处理器，返回取消订阅函数供调用方移除。 */
  function onNotification(handler: NotificationHandler) {
    handlers.push(handler)
    // 返回取消订阅函数
    return () => {
      const index = handlers.indexOf(handler)
      if (index > -1) {
        handlers.splice(index, 1)
      }
    }
  }

  /** 订阅频道，仅在连接打开时发送 subscribe 消息。 */
  function subscribe(channels: string | string[]) {
    const channelList = Array.isArray(channels) ? channels : [channels]
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'subscribe',
        channels: channelList
      }))
    }
  }

  /** 取消订阅频道，仅在连接打开时发送 unsubscribe 消息。 */
  function unsubscribe(channels: string | string[]) {
    const channelList = Array.isArray(channels) ? channels : [channels]
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'unsubscribe',
        channels: channelList
      }))
    }
  }

  onMounted(() => {
    connect()
  })

  onUnmounted(() => {
    disconnect()
  })

  return {
    isConnected,
    lastNotification,
    subscribedChannels,
    connect,
    disconnect,
    onNotification,
    subscribe,
    unsubscribe
  }
}
