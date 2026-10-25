<template>
  <AppCard padding="lg">
    <div class="flex flex-col gap-1 mb-4">
      <h2 class="font-medium text-content">系统能力</h2>
      <p class="text-sm text-content-muted">
        以下能力由部署配置控制，此处只展示状态，避免误以为已生效。
      </p>
    </div>

    <AppSkeleton v-if="loading" :rows="5" />

    <div v-else-if="features?.length" class="divide-y divide-line-subtle">
      <div
        v-for="feature in features"
        :key="feature.key"
        class="py-3 first:pt-0 last:pb-0"
      >
        <div class="flex flex-wrap items-center gap-2">
          <span class="text-sm font-medium text-content">{{ feature.title }}</span>
          <AppBadge :variant="feature.enabled ? 'success' : 'neutral'">
            {{ feature.enabled ? '已启用' : '未启用' }}
          </AppBadge>
          <AppBadge :variant="riskVariant(feature.risk)">
            {{ riskLabel(feature.risk) }}
          </AppBadge>
        </div>
        <p class="text-sm text-content-muted mt-1">{{ feature.note }}</p>
        <div class="flex flex-wrap items-center gap-x-2 gap-y-1 mt-1.5 text-xs text-content-subtle">
          <span>开启方式：部署环境变量</span>
          <code class="px-1.5 py-0.5 rounded bg-surface-strong text-content-tertiary">{{ feature.env_var }}</code>
          <span v-if="feature.scope === 'admin'">· 需管理员操作</span>
        </div>
      </div>
    </div>

    <AppStateView
      v-else
      state="empty"
      title="暂无能力状态"
      description="后端未返回功能开关信息"
      size="sm"
      padding="sm"
    />
  </AppCard>
</template>

<script setup lang="ts">
import { AppBadge, AppCard, AppSkeleton, AppStateView } from '@/components/ui'
import type { FeatureRisk, FeatureStatus } from '@/queries/features'

withDefaults(defineProps<{
  features?: FeatureStatus[]
  loading?: boolean
}>(), {
  features: undefined,
  loading: false
})

/** 风险等级徽标配色：高风险用危险色，中风险用警示色。 */
function riskVariant(risk: FeatureRisk): 'info' | 'warning' | 'danger' {
  if (risk === 'high') return 'danger'
  if (risk === 'medium') return 'warning'
  return 'info'
}

/** 风险等级文案。 */
function riskLabel(risk: FeatureRisk): string {
  if (risk === 'high') return '高风险'
  if (risk === 'medium') return '需谨慎'
  return '低风险'
}
</script>
