import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 把 /api 代理到后端，前端始终用同源相对路径。
    // 这样开发与生产的请求写法一致，也无需依赖 CORS 配置。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
