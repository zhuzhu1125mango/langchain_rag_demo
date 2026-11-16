import { computed, ref, watch } from 'vue'
import { useAppStore } from '@/stores/app'
import { useAvailableModels } from '@/queries/models'

/**
 * 对话页的模型选择状态（列表 + 当前选择 + 跨会话记忆）。
 *
 * 为什么记忆放在 localStorage 而不是给用户表加字段：
 * 这是一个纯前端偏好，不需要跨设备一致，也不需要审计。加字段意味着多一次迁移、
 * 多一套读写接口、多一个「何时写」的问题（每发一条消息都写一次 DB 显然是浪费）。
 * 什么时候该升级成用户级偏好？当出现「网页选了 A、手机上也应该是 A」的真实需求时。
 *
 * 但 localStorage 有三个必须处理的坑，否则会变成难查的 bug：
 * 1. **按用户隔离**：公用浏览器上，A 的选择不能留给 B；
 * 2. **目录到了才校验**：管理员可能已经把某个模型关掉，留着旧选择会让下一条消息
 *    直接 400，而用户完全不知道为什么；
 * 3. **拉取失败不清选择**：网络抖动不该把用户的偏好抹掉。
 */

const STORAGE_PREFIX = 'kb.selected_model:'

export function useModelSelection() {
  const appStore = useAppStore()
  const { data: catalog, isSuccess } = useAvailableModels()

  /** 可选的模型（后端已过滤 user_selectable + chat 能力） */
  const models = computed(() => catalog.value?.models ?? [])
  /** answer 路由的默认模型：不传 model_id 时实际用的那个 */
  const defaultModelId = computed(() => catalog.value?.default_model_id ?? '')

  /** 空串 = 用默认模型。不设 null：模板里 `:model-value` 绑定 select 需要字符串。 */
  const selectedModelId = ref('')

  function storageKey(): string {
    // 未登录时也会渲染对话页（只读场景），用 anonymous 兜底而不是跳过写入
    return `${STORAGE_PREFIX}${appStore.currentUser?.id ?? 'anonymous'}`
  }

  function readStored(): string {
    try {
      return localStorage.getItem(storageKey()) ?? ''
    } catch {
      return ''
    }
  }

  // 换用户 / 登出时重新读：否则上一个账号的选择会留在下一个账号的输入框里
  watch(
    () => storageKey(),
    () => {
      selectedModelId.value = readStored()
    },
    { immediate: true }
  )

  // 选择变化即落盘；清空（回到默认）时删掉键，而不是存空串
  watch(selectedModelId, value => {
    try {
      if (value) localStorage.setItem(storageKey(), value)
      else localStorage.removeItem(storageKey())
    } catch {
      // 隐私模式下写入会抛错：选择只是不持久化，不影响本次会话
    }
  })

  // 目录就绪后校验选择仍然有效。只在拉取成功时校验——失败多半是网络问题，
  // 此时清空等于「因为一次超时把用户的偏好抹掉」。
  watch(
    [models, isSuccess, selectedModelId],
    () => {
      if (!isSuccess.value) return
      const id = selectedModelId.value
      if (!id) return
      if (!models.value.some(m => m.id === id)) {
        selectedModelId.value = ''
      }
    },
    { immediate: true }
  )

  return { models, defaultModelId, selectedModelId }
}
