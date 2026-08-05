import { resolve } from 'node:path'
import { defineConfig } from 'electron-vite'

export default defineConfig({
  main: {
    build: {
      rollupOptions: { input: resolve(__dirname, 'electron/main/index.ts') },
    },
  },
  preload: {
    build: {
      rollupOptions: {
        input: {
          index: resolve(__dirname, 'electron/preload/index.ts'),
          diary: resolve(__dirname, 'electron/preload/diary.ts'),
          observability: resolve(__dirname, 'electron/preload/observability.ts'),
          settings: resolve(__dirname, 'electron/preload/settings.ts'),
        },
      },
    },
  },
  renderer: {
    root: resolve(__dirname, 'electron/renderer'),
    // 绑 IPv4：默认的 localhost 在 Windows 上只监听 ::1，
    // 某些环境连不上，表现为 Electron 窗口一片空白
    server: { host: '127.0.0.1' },
    // 直接把项目的 assets/ 当静态根，省掉一次拷贝。
    // 立绘素材动辄几十兆且会频繁重跑，拷来拷去只会拖慢开发循环
    publicDir: resolve(__dirname, 'assets'),
    build: {
      rollupOptions: {
        input: {
          // 桌宠挂件、工具窗口，以及只负责取得前台窗口单帧的隐藏捕获页。
          // 少列一个的话，生产构建里那个页面直接 404 —— 而 dev 模式下
          // Vite 会即时编译任意路径，所以这类遗漏只在打包后才暴露
          index: resolve(__dirname, 'electron/renderer/index.html'),
          capture: resolve(__dirname, 'electron/renderer/capture.html'),
          diary: resolve(__dirname, 'electron/renderer/diary.html'),
          observability: resolve(__dirname, 'electron/renderer/observability.html'),
          settings: resolve(__dirname, 'electron/renderer/settings.html'),
        },
      },
    },
  },
})
