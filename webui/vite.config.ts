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
    // 代理键是前缀匹配，不是路径段匹配：'/prompts' 并不覆盖 '/prompt-records'，
    // 后者曾因此漏配——请求落在 dev server 上被 SPA 回退成 index.html，前端拿到
    // HTML 去 JSON.parse，面板只报一句解析失败，看不出是代理问题。新增后端路由时
    // 按完整前缀补一条；'/settings/config' 写全路径，避免连 SPA 的 /settings 页面
    // 一起代理走。
    proxy: {
      '/auth': 'http://127.0.0.1:7999',
      '/api': 'http://127.0.0.1:7999',
      '/streams': 'http://127.0.0.1:7999',
      '/observability': 'http://127.0.0.1:7999',
      '/stages': 'http://127.0.0.1:7999',
      '/events': 'http://127.0.0.1:7999',
      '/prompts': 'http://127.0.0.1:7999',
      '/prompt-records': 'http://127.0.0.1:7999',
      '/models': 'http://127.0.0.1:7999',
      '/settings/config': 'http://127.0.0.1:7999',
      '/system/restart': 'http://127.0.0.1:7999',
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
    // 字体一律产出为独立文件，不因体积小被内联成 data: URI。
    // 现象：JetBrains Mono 的西里尔子集不足 4 KB，被内联进 CSS 后，页面 CSP
    //   （default-src 'self'，未单列 font-src）按 default-src 拦掉该字体，控制台
    //   每次加载都报 "violates the following Content Security Policy directive"。
    // 原因：assetsInlineLimit 默认 4096 字节，命中的资源改写为 data: URI，字体来源
    //   由此从同源变成 data:，与 index.html 声明的「字体同源自托管」不一致。
    // 后果：另一条路是在 CSP 里放开 font-src data:，等于为一个用不到的子集放宽全站
    //   字体来源；这里改为让构建产物符合既有策略，CSP 保持严格。
    assetsInlineLimit: (filePath: string) =>
      filePath.endsWith('.woff2') || filePath.endsWith('.woff') ? false : undefined,
  },
})
