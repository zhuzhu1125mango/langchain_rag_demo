/**
 * useModelSelection：`thinkingControl` 的判定。
 *
 * 这一项决定深度思考开关能不能点，三个取值对应三种不同的界面表现：
 * `off`（不会思考）/ `switchable`（能开关）/ `always_on`（会思考但关不掉）。
 * 后两者都必须置灰，但提示语不同，所以这里是枚举而不是布尔。
 *
 * 输入有两个来源——选中的模型、以及「没选模型时用的默认模型」。后者的坑是：
 * 默认模型可以不对外开放，于是它不在 `models` 里，前端只能靠目录额外给的
 * `default_thinking_control`。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref, nextTick, type Ref } from 'vue'
import { createPinia, setActivePinia } from 'pinia'

const catalog: Ref<unknown> = ref(null)
const isSuccess: Ref<boolean> = ref(false)

vi.mock('@/queries/models', () => ({
  useAvailableModels: () => ({ data: catalog, isSuccess })
}))

import { useModelSelection } from '@/composables/useModelSelection'

function model(id: string, control: string, isDefault = false) {
  return {
    id,
    display_name: id,
    capabilities: control === 'off' ? ['chat'] : ['chat', 'reasoning'],
    context_window: 32768,
    is_default: isDefault,
    thinking_control: control
  }
}

function setCatalog(payload: unknown, success = true) {
  catalog.value = payload
  isSuccess.value = success
}

describe('useModelSelection.thinkingControl', () => {
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

  it('目录还没回来时是 null（未知），不能当成「不能点」', () => {
    const { thinkingControl } = useModelSelection()
    expect(thinkingControl.value).toBeNull()
  })

  it('选中的模型可开关 → switchable', () => {
    setCatalog({
      models: [model('a', 'switchable'), model('b', 'off', true)],
      default_model_id: 'b',
      default_thinking_control: 'off'
    })
    const { thinkingControl, selectedModelId } = useModelSelection()
    selectedModelId.value = 'a'

    expect(thinkingControl.value).toBe('switchable')
  })

  it('选中的模型不会思考 → off', () => {
    setCatalog({
      models: [model('a', 'switchable'), model('b', 'off', true)],
      default_model_id: 'b',
      default_thinking_control: 'off'
    })
    const { thinkingControl, selectedModelId } = useModelSelection()
    selectedModelId.value = 'b'

    expect(thinkingControl.value).toBe('off')
  })

  it('选中会思考但关不掉的模型 → always_on（同样要置灰）', () => {
    setCatalog({
      models: [model('a', 'always_on', true)],
      default_model_id: 'a',
      default_thinking_control: 'always_on'
    })
    const { thinkingControl, selectedModelId } = useModelSelection()
    selectedModelId.value = 'a'

    expect(thinkingControl.value).toBe('always_on')
  })

  it('没选模型时按默认模型判断（默认在列表里）', () => {
    setCatalog({
      models: [model('a', 'switchable'), model('b', 'off', true)],
      default_model_id: 'b',
      default_thinking_control: 'off'
    })
    const { thinkingControl } = useModelSelection()

    expect(thinkingControl.value).toBe('off')
  })

  it('默认模型没开放给用户（不在列表）时用目录字段，而不是猜', () => {
    setCatalog({
      models: [model('a', 'off')],
      default_model_id: 'hidden',
      default_thinking_control: 'always_on'
    })
    const { thinkingControl } = useModelSelection()

    expect(thinkingControl.value).toBe('always_on')
  })

  it('没有默认路由时是 null：可控性无从判断', () => {
    setCatalog({
      models: [model('a', 'off')],
      default_model_id: '',
      default_thinking_control: ''
    })
    const { thinkingControl } = useModelSelection()

    expect(thinkingControl.value).toBeNull()
  })
})

describe('useModelSelection.nextModelId', () => {
  beforeEach(() => {
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

  function seed(): ReturnType<typeof useModelSelection> {
    setCatalog({
      models: [model('a', 'switchable'), model('b', 'off', true)],
      default_model_id: 'b',
      default_thinking_control: 'off'
    })
    return useModelSelection()
  }

  it('按「默认 → 各模型 → 默认」循环', () => {
    const h = seed()

    expect(h.nextModelId()).toBe('a')
    h.selectedModelId.value = 'a'
    expect(h.nextModelId()).toBe('b')
    h.selectedModelId.value = 'b'
    expect(h.nextModelId()).toBe('')
  })

  it('选中的模型已被关掉（不在列表里）时回到默认，而不是卡住', () => {
    const h = seed()
    h.selectedModelId.value = 'removed'

    expect(h.nextModelId()).toBe('')
  })

  it('没有可选模型时只有默认一项，切换是空操作', () => {
    setCatalog({ models: [], default_model_id: '', default_thinking_control: '' })
    const h = useModelSelection()

    expect(h.nextModelId()).toBe('')
  })
})

describe('选择记忆', () => {
  beforeEach(() => {
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
    catalog.value = {
      models: [model('a', 'switchable'), model('b', 'off', true)],
      default_model_id: 'b',
      default_thinking_control: 'off'
    }
    isSuccess.value = true
  })

  it('选择落盘后，重新创建组合式函数仍读回同一个模型', async () => {
    const first = useModelSelection()
    first.selectedModelId.value = 'a'
    await nextTick()

    const second = useModelSelection()

    expect(second.selectedModelId.value).toBe('a')
  })

  it('切回默认时删键而不是存空串——否则「默认」和「没选过」会变成两种状态', async () => {
    const first = useModelSelection()
    first.selectedModelId.value = 'a'
    await nextTick()
    first.selectedModelId.value = ''
    await nextTick()

    expect(Object.keys(localStorage)).toHaveLength(0)
  })
})
