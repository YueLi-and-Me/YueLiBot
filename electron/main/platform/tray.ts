/**
 * 创建系统托盘图标、菜单和桌宠主进程的用户入口。
 *
 * 菜单动作负责打开各窗口、切换前台采集和退出应用；具体窗口创建委托给同目录
 * 模块，后端重启由主进程转成一次 `/system/restart` 请求，由后端自己完成。
 */
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { app, Menu, Notification, Tray, nativeImage, screen, type BrowserWindow, type MenuItemConstructorOptions } from 'electron'

/** 系统托盘为无边框桌宠提供显示、窗口、配置、重启和退出入口。 */

export interface TrayHandlers {
  /** 打开输入栏并将焦点交给聊天输入框。 */
  talk(): void
  /** 将桌宠窗口恢复到工作区右下角的默认位置。 */
  resetPosition(): void
  /** 打开日记窗口。 */
  openDiary(): void
  /** 打开设置窗口。 */
  openSettings(): void
  /** 重启 Python 后端，应用新的配置或恢复异常后端。 */
  restartBackend(): void
  /** 返回是否启用持续屏幕采集。 */
  watchingScreen(): boolean
  /** 设置持续屏幕采集开关。 */
  setWatchingScreen(on: boolean): void
}

let tray: Tray | null = null

/**
 * 显示运行时故障通知。
 *
 * @param botName 通知标题中的角色名称；空字符串时使用 ``Bot``。
 * @param message 展示给用户的故障说明文本。
 * @returns {void} 通知提交后不返回值。
 * @sideEffects 优先显示托盘气泡；托盘不可用时创建并显示 Electron Notification。
 *
 * 故障可能发生在桌宠窗口创建完成之前（当前唯一的调用点是与后端失联）；托盘存在时用
 * Windows 气泡通知，否则退回 Electron 系统通知，确保启动期错误也能被看见。
 */
export function notifyTray(botName: string, message: string): void {
  const title = botName || 'Bot'
  if (process.platform === 'win32' && tray && !tray.isDestroyed()) {
    tray.displayBalloon({ title, content: message, iconType: 'warning' })
    return
  }
  new Notification({ title, body: message }).show()
}

/**
 * 在开发和生产目录中查找指定尺寸的托盘图标。
 *
 * @param size 候选图标尺寸，单位为像素。
 * @returns 第一个存在的 PNG 文件绝对路径；所有候选均不存在时返回 ``null``。
 * @sideEffects 读取文件系统存在性，不创建或修改文件。
 *
 * 两处都要试：开发时素材在项目根的 `assets/`，而生产构建时 Vite 把
 * `assets/` 的**内容**拷进了 `out/renderer/`（它是 publicDir）——
 * 只写一条路径必然有一种模式下托盘变成空白方块。
 */
function iconPath(size: number): string | null {
  const candidates = [
    join(__dirname, '../renderer', 'ui', `tray-${size}.png`),
    join(app.getAppPath(), 'assets', 'ui', `tray-${size}.png`),
  ]
  return candidates.find((p) => existsSync(p)) ?? null
}

/**
 * 根据主显示器 DPI 选择并缩放托盘图标。
 *
 * @returns {Electron.NativeImage} 按当前 DPI 选择的 16 像素逻辑尺寸图标；找不到素材时返回空图标。
 * @remarks 高 DPI 屏幕优先读取更大的源图再降采样，降低托盘图标的缩放模糊；函数只读取文件，不写入资源。
 */
function trayIcon(): Electron.NativeImage {
  const size = screen.getPrimaryDisplay().scaleFactor > 1.25 ? 64 : 32
  const p = iconPath(size) ?? iconPath(32) ?? iconPath(16)
  if (!p) return nativeImage.createEmpty()
  const img = nativeImage.createFromPath(p)
  // 托盘要的是小图标，交给 Electron 按逻辑尺寸缩放，避免各系统各自为政
  return img.isEmpty() ? nativeImage.createEmpty() : img.resize({ width: 16, height: 16 })
}

/**
 * 创建系统托盘并绑定窗口与后端控制动作。
 *
 * @param win 桌宠 BrowserWindow，用于显示状态和显隐切换；桌宠关闭时为 ``null``，
 *   托盘只保留日记、设置和后端控制等窗口无关的入口。
 * @param botName 菜单标题中的人物名称；空字符串时使用 ``Bot``。
 * @param handlers 托盘动作回调集合，由主进程注入具体业务实现。
 * @returns 当前创建的 Tray 实例。
 * @throws Error Electron 创建托盘图标失败时由运行时抛出。
 * @sideEffects 销毁旧托盘、创建新菜单并监听窗口 show/hide 和托盘 click 事件。
 */
export function createTray(win: BrowserWindow | null, botName: string, handlers: TrayHandlers): Tray {
  destroyTray()

  tray = new Tray(trayIcon())
  tray.setToolTip(botName || 'Bot')

  /**
   * 根据窗口显隐状态重建托盘菜单。
   *
   * @returns {void} 菜单更新完成或托盘已销毁时无返回值。
   * @remarks 菜单文案、复选框状态和显示/隐藏动作依赖当前窗口状态，因此窗口
   *   ``show``、``hide`` 事件和托盘单击后都必须重新构建；重复构建只替换菜单对象。
   */
  const rebuild = (): void => {
    if (!tray || tray.isDestroyed()) return
    const visible = !!win && !win.isDestroyed() && win.isVisible()
    const labelName = botName || 'Bot'

    // 桌宠窗口不存在时（desktop_pet.enabled = false）隐藏全部窗口相关菜单项，
    // 留下的入口都不依赖窗口：日记走 HTTP、设置与后端控制由主进程直接处理。
    const petItems: MenuItemConstructorOptions[] = win
      ? [
        {
          label: `${visible ? '隐藏' : '显示'}${labelName}`,
          click: () => {
            togglePet(win, !visible)
            rebuild()
          },
        },
        { label: `跟${labelName}说话`, click: handlers.talk, enabled: visible },
        {
          // 默认仅按聊天请求采集屏幕；勾选后由主进程持续提交视觉采样。
          label: `让${labelName}看着屏幕`,
          type: 'checkbox' as const,
          checked: handlers.watchingScreen(),
          click: (item) => {
            handlers.setWatchingScreen(item.checked)
            rebuild()
          },
        },
      ]
      : []

    tray.setContextMenu(
      Menu.buildFromTemplate([
        ...petItems,
        // 日记窗口与桌宠显隐状态独立，隐藏桌宠时仍允许打开日记。
        { label: `看${labelName}的日记…`, click: handlers.openDiary },
        { type: 'separator' },
        { label: '设置…', click: handlers.openSettings },
        { label: `重启${labelName}`, click: handlers.restartBackend },
        { type: 'separator' },
        ...(win ? [{ label: '搬回原位', click: handlers.resetPosition }] : []),
        {
          label: '开机自启',
          type: 'checkbox',
          checked: app.getLoginItemSettings().openAtLogin,
          click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked }),
        },
        { type: 'separator' },
        { label: '退出', click: () => app.quit() },
      ]),
    )
  }

  rebuild()
  // 菜单项文案依赖当前显隐状态，因此窗口状态变化后必须重建菜单。
  win?.on('show', rebuild)
  win?.on('hide', rebuild)

  // 左键单击直接切换显隐，右键菜单保留其他控制项。
  tray.on('click', () => {
    if (!win || win.isDestroyed()) return
    togglePet(win, !win.isVisible())
    rebuild()
  })

  return tray
}

/**
 * 切换桌宠窗口的显示状态。
 *
 * @param win 桌宠 BrowserWindow。
 * @param show 为 ``true`` 时显示窗口，为 ``false`` 时隐藏窗口。
 * @returns 无返回值；窗口已销毁时安全返回。
 * @sideEffects 显示时使用 ``showInactive`` 保留当前输入窗口焦点，隐藏时调用
 * Electron 的 ``hide``。
 */
export function togglePet(win: BrowserWindow, show: boolean): void {
  if (win.isDestroyed()) return
  // showInactive 只显示窗口而不抢占用户当前输入焦点。
  if (show) win.showInactive()
  else win.hide()
}

/**
 * 将桌宠窗口定位到工作区右下角的默认位置。
 *
 * @param win 待定位的桌宠窗口；已销毁窗口不会继续操作。
 * @param margin 窗口与工作区右下边缘的间距，默认 40 个屏幕像素，必须为非负数。
 * @returns {void} 定位完成或窗口已销毁后不返回值。
 * @sideEffects 读取主显示器工作区并修改窗口位置与尺寸。
 *
 * 使用 setBounds 并显式固定尺寸，与拖动流程复用同一条路径 ——
 * setPosition 在非整数 DPI 缩放下每次调用都会把窗口撑大 1px。
 */
export function resetPetPosition(win: BrowserWindow, margin = 40): void {
  if (win.isDestroyed()) return
  const { workArea } = screen.getPrimaryDisplay()
  const b = win.getBounds()
  win.setBounds({
    x: workArea.x + workArea.width - b.width - margin,
    y: workArea.y + workArea.height - b.height - margin,
    width: b.width,
    height: b.height,
  })
  if (!win.isVisible()) win.showInactive()
}

/** 图标文件缺失或路径不对时托盘会显示成空白方块 —— 供自检断言。 */
/**
 * 检查当前托盘图标是否为空，供启动自检确认资源路径有效。
 *
 * @returns 未找到或无法解码图标时为 ``true``。
 */
export function trayIconEmpty(): boolean {
  return trayIcon().isEmpty()
}

/**
 * 销毁当前托盘并清理模块级引用。
 *
 * @returns 无返回值；托盘不存在或已销毁时安全返回。
 * @sideEffects 移除系统托盘图标和相关事件监听器。
 */
export function destroyTray(): void {
  if (tray && !tray.isDestroyed()) tray.destroy()
  tray = null
}
