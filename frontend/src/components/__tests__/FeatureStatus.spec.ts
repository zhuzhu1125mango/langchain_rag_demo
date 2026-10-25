import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import FeatureStatusPanel from '../settings/FeatureStatusPanel.vue'
import FeatureNotice from '../settings/FeatureNotice.vue'
import type { FeatureStatus } from '@/queries/features'

function feature(overrides: Partial<FeatureStatus> = {}): FeatureStatus {
  return {
    key: 'wiki_compile',
    title: 'LLM-Wiki 编译',
    enabled: false,
    scope: 'admin',
    risk: 'low',
    env_var: 'WIKI_COMPILE_ENABLED',
    note: '将文档预编译为结构化 Wiki 页面。',
    ...overrides
  }
}

describe('FeatureStatusPanel', () => {
  it('展示启用状态、风险等级与运维开关', () => {
    const wrapper = mount(FeatureStatusPanel, {
      props: {
        features: [
          feature({ enabled: true }),
          feature({
            key: 'agent_web_fetch',
            title: 'Agent 网页抓取',
            risk: 'high',
            env_var: 'AGENT_WEB_FETCH_ENABLED',
            note: '存在 SSRF/外泄风险。'
          })
        ]
      }
    })

    expect(wrapper.text()).toContain('系统能力')
    expect(wrapper.text()).toContain('已启用')
    expect(wrapper.text()).toContain('未启用')
    expect(wrapper.text()).toContain('低风险')
    expect(wrapper.text()).toContain('高风险')
    expect(wrapper.text()).toContain('WIKI_COMPILE_ENABLED')
    expect(wrapper.text()).toContain('AGENT_WEB_FETCH_ENABLED')
    expect(wrapper.text()).toContain('需管理员操作')
  })

  it('加载中展示骨架，空数据展示空态', () => {
    const loading = mount(FeatureStatusPanel, { props: { loading: true } })
    expect(loading.find('.skeleton').exists()).toBe(true)

    const empty = mount(FeatureStatusPanel, { props: { features: [] } })
    expect(empty.text()).toContain('暂无能力状态')
  })
})

describe('FeatureNotice', () => {
  it('仅在明确未启用时展示提示与运维开关', () => {
    const notice = mount(FeatureNotice, {
      props: {
        enabled: false,
        title: '聊天分流未开启',
        description: '实验管理可用，但问答不会自动分流。',
        envVar: 'AB_EXPERIMENT_CHAT_ENABLED'
      }
    })

    expect(notice.text()).toContain('聊天分流未开启')
    expect(notice.text()).toContain('AB_EXPERIMENT_CHAT_ENABLED')
  })

  it('已启用或尚未加载时不展示', () => {
    expect(mount(FeatureNotice, {
      props: { enabled: true, title: 't', description: 'd' }
    }).text()).toBe('')
    expect(mount(FeatureNotice, {
      props: { title: 't', description: 'd' }
    }).text()).toBe('')
  })
})
