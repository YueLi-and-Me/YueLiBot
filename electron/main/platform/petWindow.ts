/**
 * 创建透明、置顶且支持点击穿透的桌宠窗口。
 *
 * 本模块只负责 Electron 窗口属性、素材 URL 和尺寸约束；角色渲染由 renderer
 * 负责，主进程通过 preload/index.ts 暴露受限的聊天和平台操作通道。
 */
import { join } from 'node:path'
import { BrowserWindow, screen } from 'electron'

/** 桌宠窗口的 Electron 平台适配实现，隔离透明窗口和点击穿透细节。 */

export interface PetWindowOptions {
  width: number
  height: number
  /** dev 时是 vite 的 http 地址，生产时是 app:// 协议地址。两者都走 loadURL。 */
  url: string
  preload: string
}

/**
 * 创建透明、置顶、可点击穿透的桌宠窗口。
 *
 * @param opts 窗口内容尺寸、页面 URL 和 preload 脚本路径；尺寸必须为正数。
 * @returns 已配置并开始加载页面的 BrowserWindow 实例。
 * @throws Error Electron 创建窗口或加载页面时失败。
 * @sideEffects 创建窗口、注册 ready-to-show 监听器、设置虚拟桌面可见性并加载
 * renderer 页面；窗口初始不抢焦点。
 */
export function createPetWindow(opts: PetWindowOptions): BrowserWindow {
  const { workArea } = screen.getPrimaryDisplay()

  const win = new BrowserWindow({
    width: opts.width,
    height: opts.height,
    // width/height 指网页内容区，而不是含边框的外框尺寸。
    // frame:false 下两者本该一致，但在非整数 DPI 缩放（这台机器 150%，
    // 逻辑分辨率 1707×1067 自带舍入）下会差出十几像素，显式声明省掉歧义
    useContentSize: true,
    // 默认放在工作区右下角，避免遮挡常用的左上角应用区域。
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
    // 必须保持可聚焦，否则输入框和 Escape 无法接收键盘事件；显示时使用
    // showInactive() 避免在普通展示场景抢占当前应用焦点。
    focusable: true,
    // 先不显示，加载完再 showInactive —— 否则创建时就会把焦点从你正在用的窗口抢走
    show: false,

    webPreferences: {
      preload: opts.preload,
      contextIsolation: true,
      nodeIntegration: false,
      // ESM preload 需要关闭 sandbox 才能注入 bridge；contextIsolation 和
      // nodeIntegration=false 仍保留渲染层与 Node API 的隔离。
      sandbox: false,
    },
  })

  // 使用 screen-saver 层级，使桌宠在全屏窗口上仍保持可见。
  win.setAlwaysOnTop(true, 'screen-saver')
  // 跟随虚拟桌面切换，避免工作区切换后窗口从当前桌面消失。
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true })

  // 默认整窗穿透，但继续转发鼠标事件，使渲染层可以根据像素命中区域切换交互。
  win.setIgnoreMouseEvents(true, { forward: true })

  // showInactive 只显示窗口而不夺取焦点，避免打断用户当前输入。
  win.once('ready-to-show', () => win.showInactive())
  win.loadURL(opts.url)

  return win
}

/**
 * 切换窗口是否接收鼠标事件。
 *
 * @param win 待修改的桌宠窗口；已销毁窗口不会继续操作。
 * @param interactive 为 true 时接收鼠标事件，为 false 时启用点击穿透。
 * @returns {void} 状态设置完成或窗口已销毁后不返回值。
 * @sideEffects 修改 BrowserWindow 的鼠标事件穿透状态，并始终保留鼠标事件转发。
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
 * @param win 待拖动的桌宠窗口；已销毁窗口不会创建定时器。
 * @returns {void} 拖动定时器创建或窗口已销毁后不返回值。
 * @sideEffects 读取当前屏幕光标和窗口边界，创建 16 毫秒间隔定时器并持续更新窗口位置；
 * 调用前会终止已有拖动任务。
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
    // [WORKAROUND] DPI 缩放下拖动必须固定窗口尺寸。
    // - 现象: 反复调用 setPosition 可能累积 DIP 与物理像素换算误差，导致窗口尺寸漂移。
    // - 原因: 拖动定时器每 16ms 更新一次，尺寸误差会在短时间内累加并触发系统边界钳制。
    // - 当前处理: 使用 setBounds 同时传入固定 width/height，仅更新 x/y。
    win.setBounds({ x: p.x - dx, y: p.y - dy, width: start.width, height: start.height })
  }, 16)
}

/**
 * 停止当前桌宠窗口的拖动定时器。
 *
 * @returns 无返回值；未处于拖动状态时安全返回。
 * @sideEffects 清除模块级定时器引用，防止窗口继续跟随光标移动。
 */
export function endDrag(): void {
  if (dragTimer) clearInterval(dragTimer)
  dragTimer = null
}

/**
 * 根据输入栏状态设置桌宠窗口焦点。
 *
 * @param {BrowserWindow} win 待更新焦点状态的桌宠窗口。
 * @param {boolean} focus 为 true 时请求窗口获得焦点；为 false 时主动移除焦点。
 * @returns {void} 窗口已销毁或焦点操作完成后不返回值。
 * @sideEffects 调用 BrowserWindow.focus 或 BrowserWindow.blur，可能改变系统活动窗口。
 */
export function focusForInput(win: BrowserWindow, focus: boolean): void {
  if (win.isDestroyed()) return
  if (focus) win.focus()
  else win.blur()
}

/**
 * 返回桌宠渲染层使用的 preload 产物路径。
 *
 * @returns 主桌宠 preload 的绝对路径。
 */
export function resolvePreload(): string {
  // 产物是 .mjs 而非 .js —— package.json 声明了 "type": "module"，
  // electron-vite 据此给 preload 用 .mjs 扩展名
  return join(__dirname, '../preload/index.mjs')
}

/**
 * 返回日记窗口的最小只读 preload 产物路径。
 *
 * @returns {string} 日记窗口 preload 的绝对路径。
 */
export function resolveDiaryPreload(): string {
  return join(__dirname, '../preload/diary.mjs')
}

/**
 * 返回观察窗口的最小只读 preload 产物路径。
 *
 * @returns {string} 观察窗口 preload 的绝对路径。
 */
export function resolveObservabilityPreload(): string {
  return join(__dirname, '../preload/observability.mjs')
}

/**
 * 返回设置窗口的配置 bridge preload 产物路径。
 *
 * @returns {string} 设置窗口 preload 的绝对路径。
 */
export function resolveSettingsPreload(): string {
  return join(__dirname, '../preload/settings.mjs')
}

/**
 * 返回渲染层产物目录，供 ``app://`` 协议解析资源根目录。
 *
 * @returns {string} 渲染层构建产物目录的绝对路径。
 */
export function resolveRendererRoot(): string {
  return join(__dirname, '../renderer')
}
