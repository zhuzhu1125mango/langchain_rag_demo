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
          // D4 补测（useChatStream/useNotifications/trace +34 用例）后 CI 态实测
          // 基线 35/38.01/27.1/34.31——本批为基线跃升（+23 点），按「新基线-3 点
          // 缓冲」设定而非 3~5 点步进（步进会使阈值长期脱离实际）；后续批次恢复
          // 步进上调。上调必须单向、禁止回退（special-projects-2026-09.md §D3）
          statements: 32,
          branches: 35,
          functions: 24,
          lines: 31,
        },
      },
    },
  }),
)
