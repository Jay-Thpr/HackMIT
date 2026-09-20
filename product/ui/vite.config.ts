import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: { include: ['src/**/*.test.ts'] },
  server: { proxy: { '/api': process.env.FAULTLINE_UI_API_URL ?? 'http://127.0.0.1:8010' } },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes('/three/') || id.includes('/@react-three/') || id.includes('/camera-controls/')) return 'spatial'
          if (id.includes('/uplot/')) return 'charts'
        },
      },
    },
  },
})
