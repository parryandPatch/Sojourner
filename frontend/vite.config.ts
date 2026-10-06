import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The frontend is a static bundle. In development it proxies /api and /ws to the
// FastAPI backend on port 8000 so the browser sees a single origin; in production the
// Docker image is served by nginx with the same proxy rules, so no CORS setup is
// needed and the API base URL stays a relative path.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
