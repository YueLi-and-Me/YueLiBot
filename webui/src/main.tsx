/**
 * 后端观察 WebUI 的前端挂载入口。
 *
 * 在 React 渲染前先写入主题属性，避免首帧使用默认亮色主题后再切换造成闪白；
 * 随后把 App 挂载到 index.html 的 #root 容器。样式入口 index.css 在此一次性
 * 引入，由 Tailwind v4 编译。
 */
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './app/App'
import './index.css'
import { initTheme } from './lib/theme'

// 主题属性必须先于首次渲染写入 <html>，否则暗色用户会看到一帧亮色闪烁。
initTheme()

const container = document.getElementById('root')
if (!container) throw new Error('index.html 缺少 #root 挂载容器')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
