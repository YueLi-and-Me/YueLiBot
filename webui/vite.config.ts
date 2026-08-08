import { resolve } from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(__dirname),
  server: {
    host: '127.0.0.1',
    port: 5181,
    proxy: {
      '/auth': 'http://127.0.0.1:7999',
      '/streams': 'http://127.0.0.1:7999',
    },
  },
  build: {
    outDir: resolve(__dirname, '../out/webui'),
    emptyOutDir: true,
  },
})
