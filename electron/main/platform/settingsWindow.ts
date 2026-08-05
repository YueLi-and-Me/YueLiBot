import { BrowserWindow } from 'electron'

/**
 * 设置窗口。一扇窗两用：
 *  · 首次启动时强制弹出（阻塞桌宠窗口/Python 监护进程的创建），填完才继续
 *  · 之后随时能从托盘重新打开，纯编辑，不阻塞任何东西
 *
 * 跟日记窗口同一种取向：普通窗口，不透明，不置顶。
 */

let settingsWindow: BrowserWindow | null = null

export interface SettingsWindowOptions {
  url: string
  preload: string
}

export function openSettingsWindow(opts: SettingsWindowOptions): BrowserWindow {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    if (settingsWindow.isMinimized()) settingsWindow.restore()
    settingsWindow.focus()
    return settingsWindow
  }

  settingsWindow = new BrowserWindow({
    width: 560,
    height: 720,
    minWidth: 460,
    minHeight: 520,
    useContentSize: true,
    title: '月璃设置',
    backgroundColor: '#171a20',
    autoHideMenuBar: true,
    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload 在 sandbox 中会静默失效；和其它几扇窗保持一致。
      sandbox: false,
    },
  })

  settingsWindow.on('closed', () => {
    settingsWindow = null
  })

  settingsWindow.loadURL(opts.url)
  return settingsWindow
}

export function settingsWindowOpen(): boolean {
  return !!settingsWindow && !settingsWindow.isDestroyed()
}

export function closeSettingsWindow(): void {
  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.close()
  settingsWindow = null
}
