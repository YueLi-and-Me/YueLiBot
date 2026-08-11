/**
 * 配置仅渲染层开发服务器和浏览器预览构建。
 *
 * 入口资源来自 electron/renderer，开发时使用可选的 window bridge；该配置不启动
 * Electron 主进程，也不替代正式应用的 electron-vite 构建流程。
 */
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

/**
 * 只跑渲染层的 dev server，不启动 Electron。
 *
 *   npm run dev:renderer
 *
 * 调角色渲染（表情切换、眨眼节拍、口型、呼吸幅度）时用这个：
 * 浏览器里改一行存一下就能看到效果，比每次重启 Electron 快得多。
 * window.pet 在浏览器里不存在，渲染层对它一律用可选链，缺了也不会崩。
 */
export default defineConfig({
  root: resolve(__dirname, 'electron/renderer'),
  publicDir: resolve(__dirname, 'assets'),
  // 显式绑 IPv4。Vite 默认的 'localhost' 在 Windows 上会解析成 ::1，
  // 只监听 IPv6 回环 —— 部分环境（沙箱、某些防火墙策略）不允许连 ::1，
  // 表现为「ready 了却连不上」，很难看出是绑定问题
  server: { host: '127.0.0.1', port: 5180 },
})
