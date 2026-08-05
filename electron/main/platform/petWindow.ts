import { join } from 'node:path'
import { BrowserWindow, screen } from 'electron'

/**
 * 桌宠窗口。
 *
 * ★ 这个文件属于 platform 适配层：所有 Electron / Windows 特有的调用都关在这里。
 *   将来若迁到 Tauri，重写的只有 electron/main/platform/ 下这几个文件，
 *   渲染层和 core 层一行不动。
 */

export interface PetWindowOptions {
  width: number
  height: number
  /** dev 时是 vite 的 http 地址，生产时是 app:// 协议地址。两者都走 loadURL。 */
  url: string
  preload: string
}

export function createPetWindow(opts: PetWindowOptions): BrowserWindow {
  const { workArea } = screen.getPrimaryDisplay()

  const win = new BrowserWindow({
    width: opts.width,
    height: opts.height,
    // width/height 指网页内容区，而不是含边框的外框尺寸。
    // frame:false 下两者本该一致，但在非整数 DPI 缩放（这台机器 150%，
    // 逻辑分辨率 1707×1067 自带舍入）下会差出十几像素，显式声明省掉歧义
    useContentSize: true,
    // 默认贴右下角，像个真的桌面挂件而不是弹窗
    x: workArea.x + workArea.width - opts.width - 40,
    y: workArea.y + workArea.height - opts.height - 40,

    transparent: true,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    // 必须是全透明色。给不透明背景色的话，transparent 在部分驱动上会失效
    backgroundColor: '#00000000',
    // ⚠ 必须可聚焦。之前设了 focusable: false 想避免抢焦点，
    // 但不可聚焦的窗口**根本收不到键盘输入** —— 输入框弹出来却打不进字，
    // Escape 也失效，于是卡在那儿关不掉。
    // 「不抢焦点」靠下面的 showInactive() 实现，而不是靠禁掉聚焦能力
    focusable: true,
    // 先不显示，加载完再 showInactive —— 否则创建时就会把焦点从你正在用的窗口抢走
    show: false,

    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload（.mjs）只有在关闭 sandbox 时才会被加载。
      // 开着的话 preload 静默失败，window.pet 不存在 —— 表现为
      // 画面一切正常但点击穿透永远开着，角色完全点不到，极难查。
      // contextIsolation 仍然开着，渲染层拿不到 Node API，安全边界没塌。
      sandbox: false,
    },
  })

  // 置顶层级选 'screen-saver'：普通 alwaysOnTop 会被全屏游戏和视频盖住
  win.setAlwaysOnTop(true, 'screen-saver')
  // 跟随虚拟桌面切换，不然切一下工作区她就不见了
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })

  // 默认整窗穿透。forward: true 让鼠标事件仍然投递给渲染层，
  // 这样渲染层才能知道光标位置、进而判断是否压在角色身上
  win.setIgnoreMouseEvents(true, { forward: true })

  // showInactive 而不是 show：显示但不夺取焦点，
  // 你正在打字的窗口不会因为她冒出来而失焦
  win.once('ready-to-show', () => win.showInactive())
  win.loadURL(opts.url)

  return win
}

/**
 * 切换窗口是否吃鼠标事件。
 *
 * 渲染层每帧检测光标下的像素 alpha，压在角色身上时调这里恢复交互，
 * 移开则重新穿透。没有这一步，角色周围的整块透明矩形都会挡住桌面点击。
 */
export function setInteractive(win: BrowserWindow, interactive: boolean): void {
  if (win.isDestroyed()) return
  win.setIgnoreMouseEvents(!interactive, { forward: true })
}

/** 拖动状态。同一时刻只可能有一个窗口在被拖，用模块级变量足够。 */
let dragTimer: ReturnType<typeof setInterval> | null = null

/**
 * 开始拖动窗口。
 *
 * 不用 `-webkit-app-region: drag`：那个 CSS 属性会把整块区域变成拖拽把手，
 * 导致点击事件不可靠 —— 而角色身上既要能拖又要能点，两者必须区分。
 *
 * 也不用渲染层传鼠标增量：窗口移动后浏览器坐标系跟着动，
 * 增量会自我干扰，拖起来会抖。所以在主进程直接读屏幕光标绝对坐标，
 * 记住按下瞬间的「光标 - 窗口」偏移，之后保持这个偏移不变。
 */
export function beginDrag(win: BrowserWindow): void {
  endDrag()
  if (win.isDestroyed()) return

  const cursor = screen.getCursorScreenPoint()
  const start = win.getBounds()
  const dx = cursor.x - start.x
  const dy = cursor.y - start.y

  dragTimer = setInterval(() => {
    if (win.isDestroyed()) return endDrag()
    const p = screen.getCursorScreenPoint()
    // ⚠ 必须用 setBounds 并显式钉住 width/height，不能用 setPosition。
    //
    // 实测：150% DPI 缩放下 setPosition **每调一次窗口就涨 1px**
    // （60 次调用把 460×501 撑到 520×561）。原因是它内部要做
    // DIP ↔ 物理像素换算，而这台机器的逻辑分辨率 1707×1067 本身就带舍入
    // （2560/1.5 = 1706.67），误差每次累积。
    // 拖动是每 16ms 一次，几秒就肉眼可见地变大 —— 而窗口撑到撞上屏幕边缘后
    // 又会被 Windows 钳制，看起来就像「贴近右边会自动缩进」。
    win.setBounds({ x: p.x - dx, y: p.y - dy, width: start.width, height: start.height })
  }, 16)
}

export function endDrag(): void {
  if (dragTimer) clearInterval(dragTimer)
  dragTimer = null
}

/**
 * 需要打字时才把焦点拿过来。
 *
 * 平时不主动 focus，你在别的窗口打字不会被打断；
 * 只有你点了她、要输入的时候才抢焦点 —— 那是你自己的意图。
 */
export function focusForInput(win: BrowserWindow, focus: boolean): void {
  if (win.isDestroyed()) return
  if (focus) win.focus()
  else win.blur()
}

export function resolvePreload(): string {
  // 产物是 .mjs 而非 .js —— package.json 声明了 "type": "module"，
  // electron-vite 据此给 preload 用 .mjs 扩展名
  return join(__dirname, '../preload/index.mjs')
}

/** 日记与观察面板各用独立最小 preload，不能继承桌宠的交互桥。 */
export function resolveDiaryPreload(): string {
  return join(__dirname, '../preload/diary.mjs')
}

export function resolveObservabilityPreload(): string {
  return join(__dirname, '../preload/observability.mjs')
}

export function resolveSettingsPreload(): string {
  return join(__dirname, '../preload/settings.mjs')
}

/** 渲染层产物目录，供 app:// 协议做根目录。 */
export function resolveRendererRoot(): string {
  return join(__dirname, '../renderer')
}
