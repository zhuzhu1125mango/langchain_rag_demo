<template>
  <div
    v-if="enabled === false"
    class="flex items-start gap-3 rounded-xl border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 px-4 py-3"
  >
    <Info class="w-4 h-4 shrink-0 text-amber-600 dark:text-amber-400 mt-0.5" />
    <div class="min-w-0">
      <p class="text-sm font-medium text-amber-800 dark:text-amber-200">{{ title }}</p>
      <p class="text-xs text-amber-700/80 dark:text-amber-300/80 mt-0.5">{{ description }}</p>
      <p v-if="envVar" class="text-xs text-amber-700/70 dark:text-amber-300/70 mt-1">
        运维开关：<code class="px-1 py-0.5 rounded bg-amber-100/70 dark:bg-amber-900/40">{{ envVar }}</code>
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Info } from '@lucide/vue'

withDefaults(defineProps<{
  enabled?: boolean
  title: string
  description: string
  envVar?: string
}>(), {
  enabled: undefined,
  envVar: undefined
})
</script>
