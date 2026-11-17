/**
 * useModelSelection：`supportsReasoning` 的判定。
 *
 * 这一项决定深度思考开关能不能点，而它的输入有两个来源——选中的模型、
 * 以及「没选模型时用的默认模型」。后者的坑是：默认模型可以不对外开放，
 * 于是它不在 `models` 里，前端只能靠目录额外给的 `default_supports_reasoning`。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref, type Ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const catalog: Ref<unknown> = ref(null)
const isSuccess: Ref<boolean> = ref(false)

vi.mock('@/queries/models', () => ({
  useAvailableModels: () => ({ data: catalog, isSuccess })
}))

import { useModelSelection } from '@/composables/useModelSelection'

function model(id: string, caps: string[], isDefault = false) {
  return {
    id,
    display_name: id,
    capabilities: caps,
    context_window: 32768,
    is_default: isDefault
  }
}

function setCatalog(payload: unknown, success = true) {
  catalog.value = payload
  isSuccess.value = success
}

describe('useModelSelection.supportsReasoning', () => {
  beforeEach(() => {
    // app store 读主题偏好时会调 matchMedia，jsdom 没实现——补个最小桩，
    // 否则连 store 都建不起来，测的就不是这里要测的东西了。
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false
    }))
    setActivePinia(createPinia())
    localStorage.clear()
    catalog.value = null
    isSuccess.value = false
  })

  it('目录还没回来时是 null（未知），不能当成「不支持」', () => {
    const { supportsReasoning } = useModelSelection()
    expect(supportsReasoning.value).toBeNull()
  })

  it('选中的模型带 reasoning 能力 → true', () => {
    setCatalog({
      models: [model('a', ['chat', 'reasoning']), model('b', ['chat'], true)],
      default_model_id: 'b',
      default_supports_reasoning: false
    })
    const { supportsReasoning, selectedModelId } = useModelSelection()
    selectedModelId.value = 'a'

    expect(supportsReasoning.value).toBe(true)
  })

  it('选中的模型没有 reasoning 能力 → false（开关要置灰）', () => {
    setCatalog({
      models: [model('a', ['chat', 'reasoning']), model('b', ['chat'], true)],
      default_model_id: 'b',
      default_supports_reasoning: false
    })
    const { supportsReasoning, selectedModelId } = useModelSelection()
    selectedModelId.value = 'b'

    expect(supportsReasoning.value).toBe(false)
  })

  it('没选模型时按默认模型判断（默认在列表里）', () => {
    setCatalog({
      models: [model('a', ['chat', 'reasoning']), model('b', ['chat'], true)],
      default_model_id: 'b',
      default_supports_reasoning: false
    })
    const { supportsReasoning } = useModelSelection()

    expect(supportsReasoning.value).toBe(false)
  })

  it('默认模型没开放给用户（不在列表）时用目录字段，而不是猜', () => {
    setCatalog({
      models: [model('a', ['chat'])],
      default_model_id: 'hidden',
      default_supports_reasoning: true
    })
    const { supportsReasoning } = useModelSelection()

    expect(supportsReasoning.value).toBe(true)
  })

  it('没有默认路由时是 null：能力无从判断', () => {
    setCatalog({
      models: [model('a', ['chat'])],
      default_model_id: '',
      default_supports_reasoning: false
    })
    const { supportsReasoning } = useModelSelection()

    expect(supportsReasoning.value).toBeNull()
  })
})
