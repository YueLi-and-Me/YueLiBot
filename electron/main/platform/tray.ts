import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { app, Menu, Tray, nativeImage, screen, type BrowserWindow } from 'electron'

/**
 * 系统托盘。
 *
 * 桌宠没有标题栏也没有关闭按钮，托盘是它**唯一的正经入口** ——
 * 在此之前退出只能靠快捷键，一旦她被拖到屏幕外就彻底找不回来了。
 *
 * ★ 这个文件属于 platform 适配层：所有 Electron / 系统特有的调用都关在这里。
 */

export interface TrayHandlers {
  /** 唤起输入栏跟她说话。 */
  talk(): void
  /** 把她搬回默认位置 —— 拖到屏幕外之后唯一的找回办法。 */
  resetPosition(): void
  /** 打开日记窗口。 */
  openDiary(): void
  /** 打开仅供开发验收的内部状态观察面板。 */
  openObservability(): void
  /** 打开设置窗口。 */
  openSettings(): void
  /** 重启 Python 后端（她卡住了，或者刚在设置里改完配置）。 */
  restartBackend(): void
  /** 她是不是正持续盯着屏幕看。 */
  watchingScreen(): boolean
  /** 打开/关闭持续屏幕感知——不想靠关键词猜的时候用它。 */
  setWatchingScreen(on: boolean): void
}

let tray: Tray | null = null

/**
 * 找到图标文件。
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

/** 图标按 DPI 选源图。150% 缩放下拿 16px 源图会糊，用大图让降采样更清楚。 */
function trayIcon(): Electron.NativeImage {
  const size = screen.getPrimaryDisplay().scaleFactor > 1.25 ? 64 : 32
  const p = iconPath(size) ?? iconPath(32) ?? iconPath(16)
  if (!p) return nativeImage.createEmpty()
  const img = nativeImage.createFromPath(p)
  // 托盘要的是小图标，交给 Electron 按逻辑尺寸缩放，避免各系统各自为政
  return img.isEmpty() ? nativeImage.createEmpty() : img.resize({ width: 16, height: 16 })
}

export function createTray(win: BrowserWindow, handlers: TrayHandlers): Tray {
  destroyTray()

  tray = new Tray(trayIcon())
  tray.setToolTip('月璃')

  const rebuild = (): void => {
    if (!tray || tray.isDestroyed()) return
    const visible = !win.isDestroyed() && win.isVisible()

    tray.setContextMenu(
      Menu.buildFromTemplate([
        {
          label: visible ? '隐藏她' : '显示她',
          click: () => {
            togglePet(win, !visible)
            rebuild()
          },
        },
        { label: '跟她说话', click: handlers.talk, enabled: visible },
        {
          // 平时只在他问起屏幕时才看一眼；打开这个就是持续看着，
          // 适合打游戏、看视频想让她陪着聊的场景。
          label: '让她看着屏幕',
          type: 'checkbox',
          checked: handlers.watchingScreen(),
          click: (item) => {
            handlers.setWatchingScreen(item.checked)
            rebuild()
          },
        },
        // 日记不依赖她显不显示 —— 想翻的时候她可能正被收着
        { label: '看她的日记…', click: handlers.openDiary },
        { type: 'separator' },
        { label: '设置…', click: handlers.openSettings },
        { label: '重启月璃', click: handlers.restartBackend },
        // 开发者验收入口，和日记严格分开，避免把数值面板做成养成功能。
        { label: '打开观察面板…', click: handlers.openObservability },
        { type: 'separator' },
        { label: '搬回原位', click: handlers.resetPosition },
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
  // 菜单项的文案取决于当前显隐状态，所以每次显隐都要重建
  win.on('show', rebuild)
  win.on('hide', rebuild)

  // 左键单击直接切显隐 —— 比每次都右键开菜单顺手
  tray.on('click', () => {
    togglePet(win, !win.isVisible())
    rebuild()
  })

  return tray
}

export function togglePet(win: BrowserWindow, show: boolean): void {
  if (win.isDestroyed()) return
  // showInactive 而不是 show：让她出现但不抢走你正在用的窗口的焦点
  if (show) win.showInactive()
  else win.hide()
}

/**
 * 搬回默认位置（工作区右下角）。
 *
 * 用 setBounds 并显式钉住尺寸，和拖动走同一条路径 ——
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
export function trayIconEmpty(): boolean {
  return trayIcon().isEmpty()
}

export function destroyTray(): void {
  if (tray && !tray.isDestroyed()) tray.destroy()
  tray = null
}
