import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    // Proxy API calls to Flask backend during local dev
    proxy: {
      '/api': 'http://localhost:5001',
      '/webhook': 'http://localhost:5001',
      '/health': 'http://localhost:5001',
    }
  }
})
