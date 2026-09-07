import process from 'node:process'
import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright 配置：三条核心链路 E2E（route mock，不依赖真实后端）。
 * 见 docs/design/frontend-test-plan.md。
 */
export default defineConfig({
  testDir: './e2e',
  /* Maximum time one test can run for. */
  timeout: 30 * 1000,
  expect: {
    /**
     * Maximum time expect() should wait for the condition to be met.
     * For example in `await expect(locator).toHaveText();`
     */
    timeout: 5000,
  },
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 2 : 0,
  /* Opt out of parallel tests on CI. */
  workers: process.env.CI ? 1 : undefined,
  /* Reporter to use. See https://playwright.dev/docs/test-reporters */
  reporter: process.env.CI ? [['html'], ['list']] : 'html',
  /* Shared settings for all the projects below. See https://playwright.dev/docs/api/class-testoptions. */
  use: {
    /* Maximum time each action such as `click()` can take. Defaults to 0 (no limit). */
    actionTimeout: 0,
    /* Base URL to use in actions like `await page.goto('/')`. */
    baseURL: process.env.CI ? 'http://localhost:4173' : 'http://localhost:5199',

    /* Collect trace when retrying the failed test. See https://playwright.dev/docs/trace-viewer */
    trace: 'on-first-retry',

    /* 统一 headless：本机 headed 窗口被遮挡时 Chromium 节流 rAF，
       Playwright 的 click stable 检查会永久挂起（元素可见却永远“不稳定”） */
    headless: true,
  },

  /* 单浏览器矩阵：三条核心链路回归以稳定为先，跨浏览器矩阵收益低 */
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
      },
    },
  ],

  /* Folder for test artifacts such as screenshots, videos, traces, etc. */
  // outputDir: 'test-results/',

  /* Run your local dev server before starting the tests */
  webServer: {
    /**
     * Use the dev server by default for faster feedback loop.
     * Use the preview server on CI for more realistic testing.
     * Playwright will re-use the local server if there is already a dev-server running.
     *
     * 本地端口用 5199：5173 常被 docker-compose 的 frontend-dev 容器占用，
     * 复用它会导致 E2E 跑在容器内旧代码上（reuseExistingServer 反而掩盖差异）。
     */
    command: process.env.CI ? 'pnpm run preview' : 'pnpm exec vite --port 5199',
    port: process.env.CI ? 4173 : 5199,
    reuseExistingServer: false,
  },
})
