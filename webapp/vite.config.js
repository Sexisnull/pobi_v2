import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 产物输出到 web/spa/，base 为 '/'。由 nginx 直接静态托管（根目录 = web/spa），
// / 即 React 控制台。旧零构建版已删除，本项目前端仅此 React 工程。
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../web/spa',
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
