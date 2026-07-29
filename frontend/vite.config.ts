import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    // Бандл едет внутрь exe — предупреждения о размере чанка не нужны.
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    // В разработке фронт живёт отдельно, API берём с backend напрямую.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8731',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
