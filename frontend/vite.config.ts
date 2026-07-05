import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 開發時把 /api 代理到 FastAPI（8000），免 CORS
export default defineConfig({
  plugins: [react()],
  // react-grid-layout 內部的 react-draggable 引用 process.env（Node 慣用），
  // 瀏覽器沒有 process → mousedown 即拋 ReferenceError，拖曳整個失效。補上空殼。
  define: { "process.env": {} },
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
})
