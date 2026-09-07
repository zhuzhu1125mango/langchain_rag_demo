/**
 * Vite 构建配置
 *
 * 主要职责：
 * - 注册 @vitejs/plugin-vue 插件以支持 .vue 单文件组件；
 * - 配置路径别名 `@` 指向 src 目录，便于模块导入；
 * - 开发服务器监听 5173 端口，并将 `/api` 请求代理到后端
 *   （默认 http://localhost:8000，可通过 VITE_API_BASE_URL 覆盖），
 *   同时开启 ws 代理以支持 WebSocket（如文档上传进度通道）。
 */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  },
  server: {
    port: 5173,
    // Docker Desktop（Windows）挂载卷的 inotify 事件不可靠，需轮询才能让 HMR 感知文件变化
    watch: {
      usePolling: true,
      interval: 1000
    },
    proxy: {
      '/api': {
        target: process.env.VITE_API_BASE_URL || 'http://localhost:8000',
        changeOrigin: true,
        ws: true,
        followRedirects: true
      }
    }
  }
})