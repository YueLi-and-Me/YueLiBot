/**
 * 创建设置窗口并处理其显示、关闭和首次启动阻塞逻辑。
 *
 * 设置页面通过 preload/settings.ts 读取和保存配置；本模块只管理窗口生命周期，
 * 配置解析与写盘由 main/config.ts 负责。
 */
import { BrowserWindow } from 'electron'

/** 设置窗口负责首次配置引导和后续编辑，使用普通不透明窗口。 */

let settingsWindow: BrowserWindow | null = null

export interface SettingsWindowOptions {
  url: string
  preload: string
  botName: string
}

/**
 * 创建或聚焦设置窗口。
 *
 * @param opts 窗口 URL、preload 脚本路径和标题中的人物名称。
 * @returns 已存在且有效的设置窗口，或新建的 BrowserWindow 实例。
 * @throws Error Electron 窗口创建或页面加载初始化失败时由运行时抛出。
 * @sideEffects 创建窗口、注册关闭清理器并加载设置页面；重复调用复用窗口。
 */
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
    title: opts.botName ? `${opts.botName}设置` : 'Bot 设置',
    backgroundColor: '#171a20',
    autoHideMenuBar: true,
    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload 在 sandbox 中无法注入配置 bridge，因此显式关闭 sandbox。
      sandbox: false,
    },
  })

  settingsWindow.on('closed', () => {
    settingsWindow = null
  })

  settingsWindow.loadURL(opts.url)
  return settingsWindow
}

/**
 * 判断设置窗口是否仍可使用。
 *
 * @returns 窗口存在且未销毁时为 ``true``。
 */
export function settingsWindowOpen(): boolean {
  return !!settingsWindow && !settingsWindow.isDestroyed()
}

/**
 * 关闭设置窗口并清理模块级引用。
 *
 * @returns 无返回值；窗口不存在或已销毁时直接返回。
 * @sideEffects 关闭 BrowserWindow，使下一次打开重新创建窗口。
 */
export function closeSettingsWindow(): void {
  if (settingsWindow && !settingsWindow.isDestroyed()) settingsWindow.close()
  settingsWindow = null
}
