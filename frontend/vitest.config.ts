import { fileURLToPath } from 'node:url'
import { mergeConfig, defineConfig, configDefaults } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      exclude: [...configDefaults.exclude, 'e2e/**'],
      root: fileURLToPath(new URL('./', import.meta.url)),
      // W6 #65：覆盖率采集与阈值（--coverage 启用；低于阈值非零退出卡点）。
      // include 收窄到逻辑层（utils/stores/composables/queries）——v8 对 .vue
      // SFC 的 sourcemap 映射失真会稀释全局覆盖率；组件覆盖靠 E2E 兜底。
      // 起步阈值从现状基线出发，随覆盖提升逐步收紧。
      coverage: {
        provider: 'v8',
        reporter: ['text', 'lcov'],
        include: ['src/utils/**/*.ts', 'src/stores/**/*.ts', 'src/composables/**/*.ts', 'src/queries/**/*.ts'],
        exclude: ['src/**/__tests__/**'],
        thresholds: {
          // 专项 D3（2026-09-30）：6/5/7/6 → 10/8/12/10。CI 实测基线
          // 11.33/9.07/14.01/10.83（54 tests，HEAD 态），各留约 1~2 点缓冲防抖动；
          // 上调必须单向、禁止回退；每次补测批次后上调 3~5 点（special-projects-2026-09.md §D3）
          statements: 10,
          branches: 8,
          functions: 12,
          lines: 10,
        },
      },
    },
  }),
)
