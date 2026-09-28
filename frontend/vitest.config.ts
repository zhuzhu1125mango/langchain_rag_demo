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
          // 实测基线 6.94/5.39/7.47/6.46，取略低于基线值防抖动；随覆盖提升收紧
          statements: 6,
          branches: 5,
          functions: 7,
          lines: 6,
        },
      },
    },
  }),
)
