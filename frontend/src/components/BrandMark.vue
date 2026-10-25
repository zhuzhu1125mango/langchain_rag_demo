<template>
  <svg
    viewBox="0 0 32 32"
    role="img"
    aria-label="知澜"
    xmlns="http://www.w3.org/2000/svg"
  >
    <defs>
      <linearGradient :id="bgId" x1="4" y1="2" x2="28" y2="30" gradientUnits="userSpaceOnUse">
        <stop stop-color="#3B82F6" />
        <stop offset="1" stop-color="#1D4ED8" />
      </linearGradient>
      <radialGradient :id="glowId" cx="0" cy="0" r="1" gradientTransform="translate(9 6) rotate(45) scale(20)">
        <stop stop-color="#FFFFFF" stop-opacity="0.34" />
        <stop offset="1" stop-color="#FFFFFF" stop-opacity="0" />
      </radialGradient>
    </defs>

    <rect width="32" height="32" rx="9" :fill="`url(#${bgId})`" />
    <rect x="0.5" y="0.5" width="31" height="31" rx="8.5" fill="none" stroke="#FFFFFF" stroke-opacity="0.16" />

    <!-- 涟漪源点：中心实心点 + 两圈扩散弧 -->
    <circle cx="16" cy="16" r="2.5" fill="#FFFFFF" />
    <circle cx="16" cy="16" r="6.4" fill="none" stroke="#FFFFFF" stroke-opacity="0.82" stroke-width="1.5" />
    <circle cx="16" cy="16" r="10.4" fill="none" stroke="#FFFFFF" stroke-opacity="0.38" stroke-width="1.2" />

    <rect width="32" height="32" rx="9" :fill="`url(#${glowId})`" />
  </svg>
</template>

<script setup lang="ts">
import { useId } from 'vue'

/** 同页多个实例时避免 SVG defs id 冲突。 */
const uid = useId().replace(/[^a-zA-Z0-9_-]/g, '')
const bgId = `zhilan-bg-${uid}`
const glowId = `zhilan-glow-${uid}`
</script>
