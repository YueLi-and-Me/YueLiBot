/**
 * 配置后端观察 WebUI 的 Vite 开发和生产构建入口。
 *
 * 构建根目录为 webui，静态入口为 index.html；该配置只处理前端资源打包，不启动
 * Python 服务或修改后端运行配置。
 */
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

export default defineConfig({
  root: resolve(__dirname),
  server: {
    host: '127.0.0.1',
    port: 5181,
    proxy: {
      '/auth': 'http://127.0.0.1:7999',
      '/api': 'http://127.0.0.1:7999',
      '/streams': 'http://127.0.0.1:7999',
    },
  },
  build: {
    outDir: resolve(__dirname, '../out/webui'),
    emptyOutDir: true,
  },
})
