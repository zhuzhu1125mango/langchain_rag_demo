<template>
  <div class="w-full" :class="avatar ? 'flex items-start gap-3' : 'space-y-2'" aria-hidden="true">
    <div v-if="avatar" class="skeleton w-10 h-10 rounded-full flex-shrink-0"></div>
    <div class="flex-1 space-y-2">
      <div
        v-for="row in rows"
        :key="row"
        class="skeleton h-4"
        :class="row === rows ? 'w-2/3' : 'w-full'"
      ></div>
    </div>
  </div>
</template>

<script setup lang="ts">
withDefaults(defineProps<{
  rows?: number
  avatar?: boolean
}>(), {
  rows: 3,
  avatar: false,
})
</script>
