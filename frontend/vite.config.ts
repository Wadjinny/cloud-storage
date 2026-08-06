import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // Keep OAuth start/callback on the Vite origin so session cookies stick.
      '/api': {
        target: 'http://127.0.0.1:1234',
        changeOrigin: true,
      },
      '/health': {
        target: 'http://127.0.0.1:1234',
        changeOrigin: true,
      },
    },
  },
})
