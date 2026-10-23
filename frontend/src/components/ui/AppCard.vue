<template>
  <div :class="classes">
    <slot />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type CardPadding = 'none' | 'sm' | 'md' | 'lg'

const props = withDefaults(defineProps<{
  padding?: CardPadding
  hoverable?: boolean
}>(), {
  padding: 'md',
  hoverable: false,
})

const PADDING: Record<CardPadding, string> = {
  none: '',
  sm: 'p-3',
  md: 'p-4',
  lg: 'p-6',
}

const classes = computed(() => [
  'bg-surface border border-line rounded-card shadow-card',
  PADDING[props.padding],
  props.hoverable ? 'transition-shadow hover:shadow-hover' : '',
])
</script>
