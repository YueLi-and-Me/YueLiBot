/**
 * 配置后端观察 WebUI 的 Vite 开发和生产构建入口。
 *
 * 构建根目录为 webui，静态入口为 index.html；该配置只处理前端资源打包，不启动
 * Python 服务或修改后端运行配置。前端技术栈为 React + Tailwind CSS，样式入口为
 * src/index.css，React 挂载入口为 src/main.tsx。
 */
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { resolve } from 'node:path'
import { defineConfig, type Plugin } from 'vite'

/**
 * 仅开发环境移除 index.html 中的严格 CSP meta。
 *
 * @returns Vite 插件实例，仅在 dev server 模式下生效。
 * @remarks
 * 现象：生产 CSP 为 `default-src 'self'`，不允许内联脚本；@vitejs/plugin-react
 * 在 dev 模式必须向 index.html 注入内联的 react-refresh preamble，否则 Fast
 * Refresh 无法工作，页面会因 CSP 拦截而空白。
 * 原因：preamble 是 Vite 转换 index.html 时注入的 inline module script，无法
 * 改为外部文件；生产构建产物不含该注入。
 * 后果：本钩子只在 `serve` 阶段生效，生产构建的 index.html 保留完整严格 CSP，
 * 不影响线上安全策略；dev server 只监听 127.0.0.1，风险可控。
 */
function relaxCspInDev(): Plugin {
  return {
    name: 'yueli-relax-csp-in-dev',
    apply: 'serve',
    transformIndexHtml(html) {
      return html.replace(/<meta\s+http-equiv="Content-Security-Policy"[^>]*>\s*/i, '')
    },
  }
}

export default defineConfig({
  root: resolve(__dirname),
  plugins: [react(), tailwindcss(), relaxCspInDev()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5181,
    proxy: {
      '/auth': 'http://127.0.0.1:7999',
      '/api': 'http://127.0.0.1:7999',
      '/streams': 'http://127.0.0.1:7999',
      '/observability': 'http://127.0.0.1:7999',
      '/stages': 'http://127.0.0.1:7999',
      '/events': 'http://127.0.0.1:7999',
      '/prompts': 'http://127.0.0.1:7999',
      '/replay': 'http://127.0.0.1:7999',
      '/ws': {
        target: 'ws://127.0.0.1:7999',
        ws: true,
      },
    },
  },
  build: {
    outDir: resolve(__dirname, '../out/webui'),
    emptyOutDir: true,
  },
})
