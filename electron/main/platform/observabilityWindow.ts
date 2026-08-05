import { BrowserWindow } from 'electron'

/**
 * 开发者观察窗口。
 *
 * 它与日记窗口刻意分开：这里允许看到数值和 JSON，但只能从托盘打开，
 * 也没有任何写入通道，不能混成面向用户的养成面板。
 */

let observabilityWindow: BrowserWindow | null = null

export interface ObservabilityWindowOptions {
  url: string
  preload: string
}

export function openObservabilityWindow(opts: ObservabilityWindowOptions): BrowserWindow {
  if (observabilityWindow && !observabilityWindow.isDestroyed()) {
    if (observabilityWindow.isMinimized()) observabilityWindow.restore()
    observabilityWindow.focus()
    return observabilityWindow
  }

  observabilityWindow = new BrowserWindow({
    width: 960,
    height: 760,
    minWidth: 680,
    minHeight: 500,
    useContentSize: true,
    title: '月璃观察面板',
    backgroundColor: '#171a20',
    autoHideMenuBar: true,
    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload 在 sandbox 中会静默失效；同日记窗口保持一致。
      sandbox: false,
    },
  })

  observabilityWindow.on('closed', () => {
    observabilityWindow = null
  })

  observabilityWindow.loadURL(opts.url)
  return observabilityWindow
}

export function observabilityWindowOpen(): boolean {
  return !!observabilityWindow && !observabilityWindow.isDestroyed()
}

export function closeObservabilityWindow(): void {
  if (observabilityWindow && !observabilityWindow.isDestroyed()) observabilityWindow.close()
  observabilityWindow = null
}
