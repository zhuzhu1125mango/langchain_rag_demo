<template>
  <div
    class="flex flex-col items-center justify-center text-center"
    :class="paddingClass"
    :role="state === 'error' ? 'alert' : 'status'"
    :aria-live="state === 'error' ? 'assertive' : 'polite'"
  >
    <template v-if="state === 'loading'">
      <AppSkeleton :rows="skeletonRows" class="max-w-md" />
    </template>

    <template v-else>
      <div
        v-if="$slots.icon || resolvedIcon"
        class="flex items-center justify-center flex-shrink-0"
        :class="iconWrapClass"
      >
        <slot name="icon">
          <component :is="resolvedIcon" :class="iconClass" />
        </slot>
      </div>
      <p :class="titleClass">{{ resolvedTitle }}</p>
      <p v-if="description" class="text-content-muted mt-1 max-w-md" :class="descriptionClass">{{ description }}</p>
      <div v-if="$slots.action" class="mt-4">
        <slot name="action" />
      </div>
      <AppButton
        v-else-if="state === 'error' && retryable"
        variant="secondary"
        size="sm"
        class="mt-4"
        @click="emit('retry')"
      >
        {{ retryText }}
      </AppButton>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { Component } from 'vue'
import { AlertCircle, Inbox } from '@lucide/vue'
import AppButton from './AppButton.vue'
import AppSkeleton from './AppSkeleton.vue'

type StateViewState = 'loading' | 'empty' | 'error'
type StateViewPadding = 'none' | 'sm' | 'md' | 'lg'
type StateViewSize = 'sm' | 'md'

const props = withDefaults(defineProps<{
  state: StateViewState
  title?: string
  description?: string
  icon?: Component
  padding?: StateViewPadding
  size?: StateViewSize
  skeletonRows?: number
  retryable?: boolean
  retryText?: string
}>(), {
  title: '',
  description: '',
  icon: undefined,
  padding: 'md',
  size: 'md',
  skeletonRows: 3,
  retryable: true,
  retryText: '重试',
})

const emit = defineEmits<{
  retry: []
}>()

const PADDING: Record<StateViewPadding, string> = {
  none: '',
  sm: 'py-6 px-3',
  md: 'py-12 px-4',
  lg: 'py-16 px-6',
}

const DEFAULT_TITLES: Record<Exclude<StateViewState, 'loading'>, string> = {
  empty: '暂无数据',
  error: '加载失败',
}

const paddingClass = computed(() => PADDING[props.padding])
const resolvedTitle = computed(() => props.title || DEFAULT_TITLES[props.state as 'empty' | 'error'])
const resolvedIcon = computed(() => props.icon || (props.state === 'error' ? AlertCircle : Inbox))
const iconWrapClass = computed(() => props.state === 'error'
  ? [
      props.size === 'sm' ? 'w-8 h-8 rounded-control mb-2' : 'w-12 h-12 rounded-panel mb-3',
      'bg-danger-soft text-danger-content',
    ]
  : [
      props.size === 'sm' ? 'w-8 h-8 rounded-control mb-2' : 'w-12 h-12 rounded-panel mb-3',
      'bg-surface-subtle text-content-subtle',
    ])
const iconClass = computed(() => (props.size === 'sm' ? 'w-4 h-4' : 'w-6 h-6'))
const titleClass = computed(() =>
  props.size === 'sm' ? 'text-xs font-medium text-content' : 'text-sm font-medium text-content'
)
const descriptionClass = computed(() => (props.size === 'sm' ? 'text-xs' : 'text-sm'))
</script>
