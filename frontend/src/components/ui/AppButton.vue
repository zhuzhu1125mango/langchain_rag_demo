<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    :class="classes"
  >
    <Loader2 v-if="loading" class="w-4 h-4 animate-spin" aria-hidden="true" />
    <slot />
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Loader2 } from '@lucide/vue'

type ButtonVariant =
  | 'primary'
  | 'primary-soft'
  | 'primary-ghost'
  | 'secondary'
  | 'ghost'
  | 'outline'
  | 'danger'
  | 'danger-ghost'
  | 'success'
type ButtonSize = 'xs' | 'sm' | 'md' | 'lg' | 'nav' | 'icon'

const props = withDefaults(defineProps<{
  variant?: ButtonVariant
  size?: ButtonSize
  type?: 'button' | 'submit' | 'reset'
  disabled?: boolean
  loading?: boolean
  block?: boolean
}>(), {
  variant: 'secondary',
  size: 'md',
  type: 'button',
  disabled: false,
  loading: false,
  block: false,
})

const SIZES: Record<ButtonSize, string> = {
  xs: 'px-2 py-1 text-xs',
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
  lg: 'px-5 py-2.5 text-sm',
  nav: 'px-2 sm:px-3 py-2 text-sm',
  icon: 'p-1.5',
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-primary-500 text-on-accent enabled:hover:bg-primary-600',
  'primary-soft':
    'bg-primary-50 text-primary-600 dark:bg-primary-900/30 dark:text-primary-400 enabled:hover:bg-primary-100 dark:enabled:hover:bg-primary-900/40',
  'primary-ghost':
    'text-primary-600 dark:text-primary-400 enabled:hover:bg-primary-50 dark:enabled:hover:bg-primary-900/30',
  secondary: 'bg-surface-subtle text-content-tertiary enabled:hover:bg-surface-strong',
  ghost: 'text-content-tertiary enabled:hover:bg-surface-hover',
  outline: 'border border-line bg-surface text-content-secondary enabled:hover:bg-surface-hover',
  danger: 'bg-danger text-on-accent enabled:hover:bg-danger-hover',
  'danger-ghost': 'text-danger-content enabled:hover:bg-danger-soft',
  success: 'bg-success text-on-accent enabled:hover:bg-success-hover',
}

const classes = computed(() => [
  'inline-flex items-center justify-center gap-2 font-medium rounded-control transition-colors',
  'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400',
  'disabled:opacity-50 disabled:cursor-not-allowed',
  SIZES[props.size],
  VARIANTS[props.variant],
  props.block ? 'w-full' : '',
])
</script>
