<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    :width="width"
    append-to-body
    align-center
    destroy-on-close
    class="app-dialog"
    v-bind="$attrs"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <slot />
    <template v-if="$slots.footer" #footer>
      <slot name="footer" />
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
defineOptions({ inheritAttrs: false })

withDefaults(defineProps<{
  modelValue: boolean
  title?: string
  width?: string
}>(), {
  title: '',
  width: '520px',
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()
</script>
