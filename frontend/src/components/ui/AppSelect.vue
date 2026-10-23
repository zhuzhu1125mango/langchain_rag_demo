<template>
  <select
    :value="modelValue"
    :disabled="disabled"
    :class="classes"
    @change="onChange"
  >
    <slot />
  </select>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type SelectSize = 'sm' | 'md'
type SelectVariant = 'default' | 'raised'

const props = withDefaults(defineProps<{
  modelValue?: string | number
  disabled?: boolean
  size?: SelectSize
  variant?: SelectVariant
}>(), {
  modelValue: '',
  disabled: false,
  size: 'sm',
  variant: 'default',
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const SIZES: Record<SelectSize, string> = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2 text-sm',
}

const VARIANTS: Record<SelectVariant, string> = {
  default: 'bg-surface-subtle border-line',
  raised: 'bg-surface-raised border-line-strong',
}

const classes = computed(() => [
  'border rounded-control text-content',
  'focus:outline-none focus:ring-2 focus:ring-primary-500',
  'disabled:opacity-50 disabled:cursor-not-allowed',
  SIZES[props.size],
  VARIANTS[props.variant],
])

function onChange(event: Event) {
  emit('update:modelValue', (event.target as HTMLSelectElement).value)
}
</script>
