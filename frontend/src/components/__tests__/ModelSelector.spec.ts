import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ModelSelector from '../chat/ModelSelector.vue'
import type { AvailableModel } from '@/queries/models'

// 默认给 `switchable`：思考由工具条上的开关控制，选项文案不带后缀，
// 这样「可见性/交互」那几组断言只关心选择器本身，不被档位文字干扰。
function model(
  id: string,
  displayName: string,
  isDefault = false,
  control: AvailableModel['thinking_control'] = 'switchable'
): AvailableModel {
  return {
    id,
    display_name: displayName,
    capabilities: control === 'off' ? ['chat', 'stream'] : ['chat', 'stream', 'reasoning'],
    context_window: 32768,
    is_default: isDefault,
    thinking_control: control
  }
}

function mountSelector(models: AvailableModel[], modelValue = '', defaultModelId = 'm1') {
  return mount(ModelSelector, {
    props: { models, modelValue, defaultModelId }
  })
}

describe('ModelSelector 可见性', () => {
  it('没有可选模型时不渲染（管理员一个都没开放）', () => {
    expect(mountSelector([]).find('select').exists()).toBe(false)
  })

  it('只有一个可选项、且它就是默认模型时不渲染——单一选项的下拉是噪音', () => {
    const wrapper = mountSelector([model('m1', '默认模型', true)], '', 'm1')
    expect(wrapper.find('select').exists()).toBe(false)
  })

  it('唯一可选项不是默认模型时仍然渲染，否则用户永远选不到它', () => {
    const wrapper = mountSelector([model('m2', '备用模型')], '', 'm1')
    expect(wrapper.find('select').exists()).toBe(true)
    // 默认模型不在列表里 → 「默认」项只能泛称，不能凭空编名字
    expect(wrapper.find('option').text()).toBe('默认 · 默认模型')
  })

  it('多个可选项时渲染，且默认项标出具体模型名', () => {
    const wrapper = mountSelector(
      [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')],
      '',
      'm1'
    )
    const options = wrapper.findAll('option')
    expect(options).toHaveLength(3)
    expect(options[0]?.text()).toBe('默认 · 通义千问 4B')
    expect(options[1]?.text()).toBe('通义千问 4B')
  })
})

describe('ModelSelector 交互', () => {
  it('切换模型时向上抛 update:modelValue', async () => {
    const wrapper = mountSelector(
      [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')],
      '',
      'm1'
    )
    await wrapper.find('select').setValue('m2')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['m2'])
  })

  it('可以切回默认（空串）——选了之后必须能回退', async () => {
    const wrapper = mountSelector(
      [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')],
      'm2',
      'm1'
    )
    await wrapper.find('select').setValue('')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([''])
  })

  it('选中态与默认态的样式不同，避免「选了但看不出来」', () => {
    const models = [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')]
    const selected = mountSelector(models, 'm2', 'm1').find('select').classes()
    const byDefault = mountSelector(models, '', 'm1').find('select').classes()

    expect(selected).toContain('text-primary-600')
    expect(byDefault).toContain('text-content-muted')
    // 互斥：两个状态不能同时带上对方的主色/灰字，否则谁生效取决于 CSS 产物顺序
    expect(byDefault).not.toContain('text-primary-600')
  })
})

describe('ModelSelector 档位标注', () => {
  it('总会思考的模型标「会思考」——否则用户只能靠猜谁慢', () => {
    const wrapper = mountSelector([model('m1', 'Qwen3 4B', true, 'always_on'), model('m2', 'X')], '', 'm1')

    expect(wrapper.findAll('option').map(o => o.text())).toContain('Qwen3 4B · 会思考')
  })

  it('不会思考的模型标「直答」', () => {
    const wrapper = mountSelector(
      [model('m1', 'Qwen2.5 7B', true, 'off'), model('m2', 'X'), model('m3', 'Y', false, 'always_on')],
      '',
      'm1'
    )

    expect(wrapper.findAll('option').map(o => o.text())).toContain('Qwen2.5 7B · 直答')
  })

  it('可开关的模型不加后缀：档位由开关控制，再写一遍是重复', () => {
    const wrapper = mountSelector([model('m1', 'Qwen3 4B', true, 'switchable'), model('m2', 'X')], '', 'm1')

    expect(wrapper.findAll('option').map(o => o.text())).toContain('Qwen3 4B')
  })

  it('「默认」项也要带档位，否则默认档位无从得知', () => {
    const wrapper = mountSelector(
      [model('m1', 'Qwen2.5 7B', true, 'off'), model('m2', 'X'), model('m3', 'Y', false, 'always_on')],
      '',
      'm1'
    )

    expect(wrapper.findAll('option')[0]?.text()).toBe('默认 · Qwen2.5 7B · 直答')
  })
})

describe('ModelSelector 两档点击即切', () => {
  const direct = () => model('m1', 'Qwen2.5 7B', true, 'off')
  const thinking = () => model('m2', 'Qwen3 4B', false, 'always_on')

  it('两个档位互斥时用按钮，而不是下拉', () => {
    const wrapper = mountSelector([direct(), thinking()], '', 'm1')

    expect(wrapper.find('button[data-tier]').exists()).toBe(true)
    expect(wrapper.find('select').exists()).toBe(false)
  })

  it('按钮上显示当前档位，而不是模型名', () => {
    const wrapper = mountSelector([direct(), thinking()], '', 'm1')
    expect(wrapper.find('button[data-tier]').text()).toBe('直答')

    const swapped = mountSelector([direct(), thinking()], 'm2', 'm1')
    expect(swapped.find('button[data-tier]').text()).toBe('会思考')
  })

  it('点一下切到「当前不是的那个」，再点切回来', async () => {
    const wrapper = mountSelector([direct(), thinking()], '', 'm1')

    await wrapper.find('button[data-tier]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['m2'])
  })

  it('当前已是思考档时，点击回到直答档', async () => {
    const wrapper = mountSelector([direct(), thinking()], 'm2', 'm1')

    await wrapper.find('button[data-tier]').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['m1'])
  })

  it('两个模型都是直答时退回下拉：按钮上「直答↔直答」没有意义', () => {
    const wrapper = mountSelector(
      [model('m1', 'Qwen2.5 7B', true, 'off'), model('m2', 'Qwen2.5 3B', false, 'off')],
      '',
      'm1'
    )

    expect(wrapper.find('button[data-tier]').exists()).toBe(false)
    expect(wrapper.find('select').exists()).toBe(true)
  })

  it('多于两个模型时退回下拉：没法用「另一个」指代', () => {
    const wrapper = mountSelector(
      [model('m1', 'A', true, 'off'), model('m2', 'B', false, 'off'), thinking()],
      '',
      'm1'
    )

    expect(wrapper.find('button[data-tier]').exists()).toBe(false)
    expect(wrapper.find('select').exists()).toBe(true)
  })

  it('悬停提示说清「当前是哪个模型、点了会变成哪个」', () => {
    const wrapper = mountSelector([direct(), thinking()], '', 'm1')
    const title = wrapper.find('button[data-tier]').attributes('title') ?? ''

    expect(title).toContain('Qwen2.5 7B')
    expect(title).toContain('Qwen3 4B')
  })
})
