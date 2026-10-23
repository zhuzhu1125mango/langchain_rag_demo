<template>
  <span :class="classes">
    <slot />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type BadgeVariant = 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'info'
type BadgeSize = 'sm' | 'md'

const props = withDefaults(defineProps<{
  variant?: BadgeVariant
  size?: BadgeSize
}>(), {
  variant: 'neutral',
  size: 'sm',
})

const SIZES: Record<BadgeSize, string> = {
  sm: 'px-1.5 py-0.5 text-xs',
  md: 'px-2 py-0.5 text-xs',
}

const VARIANTS: Record<BadgeVariant, string> = {
  neutral: 'bg-surface-strong text-content-tertiary',
  primary: 'bg-primary-50 text-primary-600 dark:bg-primary-900/30 dark:text-primary-400',
  success: 'bg-success-soft text-success-content',
  warning: 'bg-warning-soft text-warning-content',
  danger: 'bg-danger-soft text-danger-content',
  info: 'bg-blue-50 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
}

const classes = computed(() => [
  'inline-flex items-center gap-1 rounded-pill font-medium',
  SIZES[props.size],
  VARIANTS[props.variant],
])
</script>
