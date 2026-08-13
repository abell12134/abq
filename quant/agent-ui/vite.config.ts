import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Production public entry: http://<host>:8000/agent/ (webapp proxies /agent/api → :8010)
// Dev: vite :5173 with /api and /agent/api → :8010
export default defineConfig({
  plugins: [react()],
  base: '/agent/',
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
      },
      '/agent/api': {
        target: 'http://127.0.0.1:8010',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/agent\/api/, '/api'),
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    allowedHosts: true,
  },
})
