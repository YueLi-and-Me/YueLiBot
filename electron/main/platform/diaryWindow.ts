/**
 * 创建和管理只读日记窗口。
 *
 * 窗口通过 preload/diary.ts 读取 Python 后端返回的日记数据，导航、脚本和写入
 * 能力受限于本模块的 BrowserWindow 配置；托盘入口由 tray.ts 负责。
 */
import { BrowserWindow } from 'electron'

/** 日记使用普通可缩放窗口，且只允许通过 preload 读取后端数据。 */

let diaryWindow: BrowserWindow | null = null

export interface DiaryWindowOptions {
  url: string
  preload: string
  botName: string
}

/**
 * 创建或聚焦日记窗口。
 *
 * @param opts 窗口页面 URL、preload 脚本路径和标题中的人物名称。
 * @returns 已存在且有效的窗口，或新创建的 BrowserWindow 实例。
 * @throws Error Electron 窗口创建或页面加载初始化失败时由运行时抛出。
 * @sideEffects 创建窗口、注册关闭清理器并加载日记页面；重复调用只恢复并聚焦
 * 现有窗口，避免同一页面出现多个实例。
 */
export function openDiaryWindow(opts: DiaryWindowOptions): BrowserWindow {
  // 复用仍存活的窗口，保证托盘重复点击不会创建多个日记页面。
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
    // 普通窗口必须设置不透明背景，避免页面加载期间透出桌面。
    backgroundColor: '#f7f5f2',
    autoHideMenuBar: true,
    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload 在 sandbox 中无法正常注入 bridge；页面虽能加载，数据通道会缺失。
      sandbox: false,
    },
  })

  diaryWindow.on('closed', () => {
    diaryWindow = null
  })

  diaryWindow.loadURL(opts.url)
  return diaryWindow
}

/**
 * 判断日记窗口是否仍可使用。
 *
 * @returns 窗口已创建且未被 Electron 销毁时为 ``true``。
 */
export function diaryWindowOpen(): boolean {
  return !!diaryWindow && !diaryWindow.isDestroyed()
}

/**
 * 关闭日记窗口并清理模块级引用。
 *
 * @returns 无返回值；窗口不存在或已经销毁时安全返回。
 * @sideEffects 关闭 BrowserWindow，并使下一次打开调用创建新实例。
 */
export function closeDiaryWindow(): void {
  if (diaryWindow && !diaryWindow.isDestroyed()) diaryWindow.close()
  diaryWindow = null
}
