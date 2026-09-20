import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: { include: ['src/**/*.test.ts'] },
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
