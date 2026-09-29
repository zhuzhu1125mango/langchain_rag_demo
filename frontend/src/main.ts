import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// EP 暗色模式变量：配合 html.dark 使 el-* 组件跟随全局主题切换
import 'element-plus/theme-chalk/dark/css-vars.css'
import 'highlight.js/styles/github.css'

import App from './App.vue'
import router from './router'

import './style.css'

const app = createApp(App)
const pinia = createPinia()

// W6 #42：全局错误兜底——事件处理器内未捕获的异步错误不走 ErrorBoundary
//（onErrorCaptured 只捕获渲染/生命周期），此处统一记录，不让错误静默丢失
app.config.errorHandler = (err, _instance, info) => {
  console.error(`[GlobalErrorHandler] ${info}:`, err)
}

app.use(pinia)
app.use(router)
app.use(ElementPlus)
app.use(VueQueryPlugin, {
  queryClientConfig: {
    defaultOptions: {
      queries: {
        staleTime: 5 * 60 * 1000,
        refetchOnWindowFocus: false,
        retry: 1
      }
    }
  }
})

app.mount('#app')
