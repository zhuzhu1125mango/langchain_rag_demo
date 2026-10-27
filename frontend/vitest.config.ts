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
          // 专项 D3/D4（2026-09-30）：6/5/7/6 → 10/8/12/10 → 32/35/24/31。
          // SEC-15（2026-10-27）：实测基线 36.17/41.2/25.96/35.11 → 35/40/25/34
          // （按「实测-1 点」留抖动缓冲；阈值上调必须单向，禁止回退）。
          statements: 35,
          branches: 40,
          functions: 25,
          lines: 34,
        },
      },
    },
  }),
)
