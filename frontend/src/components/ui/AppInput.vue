<template>
  <textarea
    v-if="multiline"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :rows="rows"
    :maxlength="maxlength"
    :class="classes"
    @input="onInput"
  ></textarea>
  <input
    v-else
    :type="type"
    :value="modelValue"
    :placeholder="placeholder"
    :disabled="disabled"
    :maxlength="maxlength"
    :class="classes"
    @input="onInput"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'

type InputSize = 'sm' | 'md'
type InputVariant = 'default' | 'raised'

const props = withDefaults(defineProps<{
  modelValue?: string | number
  type?: string
  placeholder?: string
  disabled?: boolean
  multiline?: boolean
  rows?: number
  maxlength?: number
  size?: InputSize
  variant?: InputVariant
  modelModifiers?: Record<string, boolean>
}>(), {
  modelValue: '',
  type: 'text',
  placeholder: '',
  disabled: false,
  multiline: false,
  rows: 3,
  size: 'md',
  variant: 'default',
  modelModifiers: () => ({}),
})

const emit = defineEmits<{
  'update:modelValue': [value: string | number]
}>()

const SIZES: Record<InputSize, string> = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2 text-sm',
}

const VARIANTS: Record<InputVariant, string> = {
  default: 'bg-surface-subtle border-line',
  raised: 'bg-surface-raised border-line-strong',
}

const classes = computed(() => [
  'w-full border rounded-control text-content',
  'placeholder:text-content-subtle focus:outline-none focus:ring-2 focus:ring-primary-500',
  'disabled:opacity-50 disabled:cursor-not-allowed',
  SIZES[props.size],
  VARIANTS[props.variant],
])

function onInput(event: Event) {
  const raw = (event.target as HTMLInputElement | HTMLTextAreaElement).value
  emit('update:modelValue', props.modelModifiers?.number ? Number(raw) : raw)
}
</script>
