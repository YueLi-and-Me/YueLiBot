/**
 * 创建只读运行观察窗口并桥接后端诊断数据。
 *
 * 窗口通过 preload/observability.ts 读取状态快照、事件追踪和人物数据，不能
 * 修改业务状态；窗口生命周期由主进程托盘和 IPC 处理器协调。
 */
import { BrowserWindow } from 'electron'

/** 观察窗口展示只读诊断数据，不暴露业务状态写入通道。 */

let observabilityWindow: BrowserWindow | null = null

export interface ObservabilityWindowOptions {
  url: string
  preload: string
  botName: string
}

/**
 * 创建或聚焦运行观察窗口。
 *
 * @param opts 窗口 URL、preload 脚本路径和标题中的人物名称。
 * @returns 已存在且有效的观察窗口，或新建的 BrowserWindow 实例。
 * @throws Error Electron 窗口创建或页面加载初始化失败时由运行时抛出。
 * @sideEffects 创建只读窗口、注册关闭清理器并加载观察页面；重复调用复用窗口。
 */
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
    title: opts.botName ? `${opts.botName}观察面板` : 'Bot 观察面板',
    backgroundColor: '#171a20',
    autoHideMenuBar: true,
    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload 在 sandbox 中无法注入只读 bridge，因此显式关闭 sandbox。
      sandbox: false,
    },
  })

  observabilityWindow.on('closed', () => {
    observabilityWindow = null
  })

  observabilityWindow.loadURL(opts.url)
  return observabilityWindow
}

/**
 * 判断观察窗口是否仍可使用。
 *
 * @returns 窗口存在且未销毁时为 ``true``。
 */
export function observabilityWindowOpen(): boolean {
  return !!observabilityWindow && !observabilityWindow.isDestroyed()
}

/**
 * 关闭观察窗口并清理模块级引用。
 *
 * @returns 无返回值；窗口不存在或已销毁时直接返回。
 * @sideEffects 关闭 BrowserWindow，使下一次打开重新创建窗口。
 */
export function closeObservabilityWindow(): void {
  if (observabilityWindow && !observabilityWindow.isDestroyed()) observabilityWindow.close()
  observabilityWindow = null
}
