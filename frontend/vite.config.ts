import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 開發時把 /api 代理到 FastAPI（8000），免 CORS
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000' },
  },
})
