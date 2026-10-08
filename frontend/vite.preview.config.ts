import { mergeConfig } from 'vite'
import base from './vite.config.ts'

// 验收入口与正常开发入口使用不同端口，避免假接口进入日常使用。
export default mergeConfig(base, {
  server: { port: 5174, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:8001' } } },
})
