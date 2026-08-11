import { BrowserWindow } from 'electron'

/**
 * 日记窗口。
 *
 * 跟桌宠窗口完全相反的取向：那个是无边框、透明、置顶、点击穿透的挂件，
 * 这个是一扇**正常的窗**——有标题栏、能缩放、能最小化、不抢置顶。
 * 翻日记是一件要坐下来慢慢看的事，套用挂件那套交互只会碍事。
 *
 * ★ platform 适配层：所有 Electron 特有的调用都关在这里。
 */

let diaryWindow: BrowserWindow | null = null

export interface DiaryWindowOptions {
  url: string
  preload: string
  botName: string
}

export function openDiaryWindow(opts: DiaryWindowOptions): BrowserWindow {
  // 已经开着就聚焦回来，而不是再开一扇 —— 否则托盘点几下就堆出一排窗口
  if (diaryWindow && !diaryWindow.isDestroyed()) {
    if (diaryWindow.isMinimized()) diaryWindow.restore()
    diaryWindow.focus()
    return diaryWindow
  }

  diaryWindow = new BrowserWindow({
    width: 520,
    height: 680,
    minWidth: 380,
    minHeight: 420,
    useContentSize: true,
    title: opts.botName ? `${opts.botName}的日记` : 'Bot 日记',
    // 跟桌宠共用一套配色，但这里是普通窗口，需要一个不透明底
    backgroundColor: '#f7f5f2',
    autoHideMenuBar: true,
    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // 跟桌宠窗口同样的理由：ESM preload（.mjs）只有关掉 sandbox 才会加载。
      // 开着的话 preload **静默失败** —— 窗口正常打开、页面正常渲染，
      // 只是 window.pet 不存在，日记永远显示「还没有记下什么」。
      // 没有任何报错，非常难查。
      sandbox: false,
    },
  })

  diaryWindow.on('closed', () => {
    diaryWindow = null
  })

  diaryWindow.loadURL(opts.url)
  return diaryWindow
}

export function diaryWindowOpen(): boolean {
  return !!diaryWindow && !diaryWindow.isDestroyed()
}

export function closeDiaryWindow(): void {
  if (diaryWindow && !diaryWindow.isDestroyed()) diaryWindow.close()
  diaryWindow = null
}
