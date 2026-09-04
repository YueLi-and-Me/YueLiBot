/**
 * Electron 主进程入口，负责桌宠窗口、托盘和平台读取权限。
 *
 * 本模块在应用就绪前设置运行时目录，随后读取拆分 TOML 配置、注册设置与业务 IPC、
 * 创建桌宠窗口，并通过 {@link BackendLink} 连接**已在运行**的 Python 后端。
 * 进程入口是 Python：它按 `[desktop_pet] enabled` 决定是否拉起本外壳，并监护 QQ
 * 适配器；本模块不启动也不重启后端进程。前台窗口轮询、屏幕捕获和输入活动采集由
 * Electron 执行，记忆、人格、日程和模型调用由 Python 服务处理。
 * 渲染器仅经 preload bridge 访问本模块，IPC 类型与消息名称统一由 `shared/ipc.ts` 定义。
 */

import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { app, BrowserWindow, dialog, ipcMain, powerMonitor, screen } from 'electron'
import 'dotenv/config'

import {
  APP_CAPTURE_URL, APP_DIARY_URL, APP_INDEX_URL, APP_SETTINGS_URL,
  registerAppScheme, serveAppScheme,
} from './platform/appProtocol.ts'
import { closeDiaryWindow, diaryWindowOpen, openDiaryWindow } from './platform/diaryWindow.ts'
import { foregroundAvailable, isSelfProcess, readForeground } from './platform/foreground.ts'
import { captureScreen, captureWindow, disposeWindowCapture } from './platform/capture.ts'
import {
  beginDrag, createPetWindow, endDrag, focusForInput,
  resolveDiaryPreload, resolvePreload,
  resolveRendererRoot, resolveSettingsPreload,
  setInteractive,
} from './platform/petWindow.ts'
import { closeSettingsWindow, openSettingsWindow } from './platform/settingsWindow.ts'
import {
  createTray, destroyTray, notifyTray, resetPetPosition, togglePet, trayIconEmpty,
} from './platform/tray.ts'
import {
  assertConfigConsistent, configIsComplete, ensureAdapterConfig, ensureAdapterSelection,
  mergeLegacyEnvPrefill,
  readConfigDirectory, tryPrefillFromLegacyEnv, writeConfigDirectory,
} from './config.ts'
import { IPC, type YueliConfig } from '../shared/ipc.ts'
import { mentionsScreen } from './screenIntent.ts'
import { InputActivity } from './inputActivity.ts'
import { BackendLink } from './python/backendLink.ts'
import { PythonClient, windowSink, type EventSink } from './python/client.ts'
import { resolveRuntimePaths } from './runtimePaths.ts'

const PET_W = 440
const PET_H = 480

/** 桌宠关闭时的事件出口：推送直接丢弃；不建立 WS 时该出口实际不会收到消息。 */
const NULL_SINK: EventSink = { send: () => {}, isAlive: () => false }

/** 前台窗口轮询间隔，单位为毫秒；后端仍会根据事件内容执行业务级节流。 */
const FOREGROUND_POLL_MS = 8_000

if (process.env.YUELI_DISABLE_GPU === '1') app.disableHardwareAcceleration()

const runtimePaths = resolveRuntimePaths(
  app.getAppPath(),
  process.env.YUELI_PROJECT_ROOT,
  process.env.YUELI_DATA_DIR,
  process.env.YUELI_CONFIG_DIR,
)
for (const directory of [
  runtimePaths.dataDir,
  runtimePaths.electronUserDataDir,
  runtimePaths.electronSessionDataDir,
  runtimePaths.electronTempDir,
  runtimePaths.electronCrashDumpsDir,
]) {
  mkdirSync(directory, { recursive: true })
}
// 必须在 app.ready 之前改写 Electron 路径，避免 Chromium 先在系统用户目录创建缓存、
// Local Storage、崩溃转储和临时文件。
app.setPath('userData', runtimePaths.electronUserDataDir)
app.setPath('sessionData', runtimePaths.electronSessionDataDir)
app.setPath('temp', runtimePaths.electronTempDir)
app.setPath('crashDumps', runtimePaths.electronCrashDumpsDir)

let petWindow: BrowserWindow | null = null
let backend: BackendLink | null = null
let client: PythonClient | null = null
/**
 * 本外壳是否由 Python 后端拉起。
 *
 * 决定托盘「退出」的语义：由后端拉起时它是整个应用的唯一可见入口，退出应当连同后端
 * 一起结束；用户自己起的外壳连的是别人的后端，退出只该关掉这个客户端。
 */
const managedByBackend = process.env.YUELI_SHELL_MANAGED === '1'
/** 当前有效配置；后端重启后刷新，保证 Electron 侧按最新功能开关执行轮询和截图。 */
let currentCfg: YueliConfig | null = null
let inputActivity: InputActivity | null = null
/** 退出清理是否已完成；before-quit 的异步优雅关闭据此实现幂等重入。 */
let quitPrepared = false
/**
 * 本次运行的持续截图开关。
 *
 * 开启后每条消息都允许触发一帧截图；关闭时仅在输入命中屏幕意图时截图。该状态不写入
 * 配置文件，应用重启后恢复为关闭，避免持续采集行为跨会话隐式生效。
 */
let watchScreen = false
registerAppScheme()

/**
 * 让终端信号走与窗口关闭相同的退出链。
 *
 * 现象：在终端里按 Ctrl+C 时进程直接结束，窗口捕获与键鼠钩子来不及释放。
 * 原因：Node 对 SIGINT / SIGTERM 的默认行为是立即终止进程，`before-quit` 不会触发。
 * 后果：移除本注册会让「终端退出」与「托盘退出」两条路径的收尾行为再次分叉，
 *   而分叉只在终端里显现，日常从托盘退出时看不出来。
 *
 * 由后端拉起时本外壳在独立进程组中，终端的 Ctrl+C 不会送到这里——那条路径上是后端
 * 先收尾、再终止本进程树，与本函数无关。
 *
 * 第二次信号强制退出：本进程的收尾只涉及本地资源，没有需要保护的落盘操作。
 *
 * @returns 无返回值。
 * @sideEffects 在当前进程注册 SIGINT 与 SIGTERM 监听。
 */
function registerTerminationSignals(): void {
  let quitRequested = false
  const onSignal = (): void => {
    if (quitRequested) {
      app.exit(1)
      return
    }
    quitRequested = true
    app.quit()
  }
  process.on('SIGINT', onSignal)
  process.on('SIGTERM', onSignal)
}
registerTerminationSignals()

/**
 * 根据当前配置启用或停止输入活动采集器。
 *
 * @returns 无返回值。
 * @remarks 配置或采集器尚未初始化时直接返回；启用主动感知时启动全局采集，否则停止采集。
 * @throws 传播采集器启动或停止过程中产生的运行时错误。
 */
function syncInputActivity(): void {
  if (!inputActivity) return
  // 桌宠关闭时不采集键鼠：前台快照不会上报，全局钩子只剩隐私暴露面。
  if (currentCfg?.generation.proactive.enabled && currentCfg.desktop_pet.enabled) {
    inputActivity.start()
  } else {
    inputActivity.stop()
  }
}

const dataDir = runtimePaths.dataDir
const configDir = runtimePaths.configDir
const legacyConfigPath = runtimePaths.legacyConfigPath
/** 首次启动等设置窗口保存完成时要 resolve 的回调；非首次启动场景下始终为 null。 */
let firstRunResolve: (() => void) | null = null

app.whenReady().then(async () => {
  const devUrl = process.env.ELECTRON_RENDERER_URL
  if (!devUrl) serveAppScheme(resolveRendererRoot())
  // 启用哪个适配器只有 config/adapter.toml 一处声明，主体侧的设置页读同一份；
  // 它的连接配置与插件同目录，换协议端就是换那个文件夹，配置跟着一起走。
  // 拉起适配器的是 Python，这里只保证两份文件存在——配置文件的写入方一直是 Electron。
  ensureAdapterConfig(join(app.getAppPath(), 'adapters', ensureAdapterSelection(configDir)))

  // 配置读写 IPC 同时服务首次启动设置窗口和后续编辑。
  ipcMain.handle(IPC.ReadConfig, async () => readConfigDirectory(configDir, legacyConfigPath))
  ipcMain.handle(IPC.SaveConfig, async (_e, config: YueliConfig) => {
    try {
      // 结构校验只约束用户编辑入口；迁移写回的是兼容解析后的旧配置，不能使用更严格的编辑期规则阻断升级。
      assertConfigConsistent(config)
      writeConfigDirectory(configDir, config)
      if (firstRunResolve) {
        const resolve = firstRunResolve
        firstRunResolve = null
        resolve()
      }
      return { ok: true }
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) }
    }
  })
  ipcMain.on(IPC.RestartBackend, () => {
    const target = client
    if (!target) return
    console.log('[main] 收到重启指令，请求后端重启…')
    currentCfg = readConfigDirectory(configDir, legacyConfigPath)
    syncInputActivity()
    // 重启由后端自己完成：它优雅收尾后重新执行同一份命令行。本外壳会先失联、
    // 再由 BackendLink 重连；若本外壳也是后端拉起的，它会随后端一起被换掉。
    void target.restart().catch((error: unknown) => {
      console.error('[main] 重启请求失败：', error)
    })
  })
  // 首次启动缺少模型或 API 密钥时先显示设置窗口，桌宠窗口与后端连接延后建立。
  if (!configIsComplete(readConfigDirectory(configDir, legacyConfigPath))) {
    await runFirstRunWizard(devUrl)
  }

  await startApp(devUrl, readConfigDirectory(configDir, legacyConfigPath))
}).catch((error: unknown) => {
  const message = error instanceof Error ? error.message : String(error)
  console.error('[main] 启动初始化失败：', error)
  if (app.isReady()) dialog.showErrorBox('启动失败', message)
  app.quit()
})

/**
 * 执行首次启动配置引导：先自动迁移旧 `.env`，再按迁移后的完整性决定是否显示设置窗口。
 *
 * @param devUrl 开发服务器地址；生产模式下为 `undefined`。
 * @returns 迁移后配置已完整时立即完成；否则在设置窗口保存配置后完成。
 * @throws Error 当配置目录无法读写或设置窗口初始化失败时抛出。
 * @remarks 预填值通过合并写入配置目录，不会覆盖已有服务商、模型和任务候选；旧 `.env`
 *   无法解析时记录错误并弹出说明，但继续打开设置窗口，避免未处理的异步异常终止启动。
 */
function runFirstRunWizard(devUrl?: string): Promise<void> {
  const legacyEnvPath = join(app.getAppPath(), '.env')
  try {
    const prefill = tryPrefillFromLegacyEnv(legacyEnvPath)
    if (prefill) {
      const base = readConfigDirectory(configDir, legacyConfigPath)
      const migrated = mergeLegacyEnvPrefill(base, prefill)
      writeConfigDirectory(configDir, migrated)
      const current = readConfigDirectory(configDir, legacyConfigPath)
      console.log('[config] 迁移完毕，配置目录已更新')
      if (configIsComplete(current)) {
        console.log('[config] 迁移后配置已满足启动条件，跳过设置页，直接启动主进程')
        return Promise.resolve()
      }
      console.log('[config] 迁移后配置仍不完整，打开设置页补充')
    } else {
      console.log('[config] 迁移检查完毕，打开设置页补充缺失配置')
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    console.error(`[main] ${message}`)
    dialog.showErrorBox(
      '旧版 .env 未能自动迁移',
      `${message}

请按提示处理 .env 后，在弹出的设置页中手动完成模型配置；本次不会自动带入 .env 中的连接信息。`,
    )
  }

  return new Promise<void>((resolve) => {
    firstRunResolve = resolve
    openSettingsWindow({
      url: devUrl
        ? new URL('settings.html?mode=first-run', devUrl).href
        : `${APP_SETTINGS_URL}?mode=first-run`,
      preload: resolveSettingsPreload(),
      botName: '',
    })
  }).then(() => {
    closeSettingsWindow()
  })
}

/**
 * 创建桌宠运行时资源，注册主进程 IPC，并连接 Python 后端与前台感知循环。
 *
 * @param devUrl 开发服务器地址；生产模式下为 `undefined`，窗口使用自定义协议加载资源。
 * @param cfg 已读取且通过最小启动条件检查的运行时配置。
 * @returns 所有同步初始化完成后的 Promise；后端连接与轮询器通过事件持续运行。
 * @throws Error 当窗口、配置读取或 IPC 初始化失败时抛出。
 * @remarks 方法会创建窗口和定时器、注册应用退出清理逻辑，并对屏幕捕获失败执行隔离处理，避免阻断文本消息发送。
 */
async function startApp(
  devUrl: string | undefined,
  cfg: YueliConfig,
): Promise<void> {
  currentCfg = cfg
  inputActivity = new InputActivity()
  syncInputActivity()
  // 桌宠开关关闭时不创建窗口：托盘仍提供设置、日记与后端控制，重启应用后按新配置生效。
  petWindow = cfg.desktop_pet.enabled
    ? createPetWindow({
      width: PET_W, height: PET_H,
      url: devUrl ?? APP_INDEX_URL,
      preload: resolvePreload(),
    })
    : null

  // 窗口控制 IPC：渲染器只传递用户交互意图，窗口状态由主进程统一维护。
  ipcMain.on(IPC.SetInteractive, (_e, interactive: boolean) => {
    if (petWindow) setInteractive(petWindow, interactive)
  })
  ipcMain.on(IPC.Quit, () => app.quit())
  ipcMain.on(IPC.BeginDrag, () => petWindow && beginDrag(petWindow))
  ipcMain.on(IPC.EndDrag, () => endDrag())
  ipcMain.on(IPC.FocusInput, (_e, focus: boolean) => petWindow && focusForInput(petWindow, focus))

  // 连接后端：只连不拉。进程入口是 Python，后端已经在运行，就绪后再创建客户端。
  backend = new BackendLink({ dataDir })
  backend.on('ready', (port, token) => {
    client?.stop()
    // 桌宠关闭时窗口不存在：仍创建客户端让日记窗口走 HTTP 可用，但不建立 WS——
    // 聊天、语音、睡眠推送都没有接收窗口。
    const win = petWindow && !petWindow.isDestroyed() ? petWindow : null
    client = new PythonClient(port, token, win ? windowSink(win) : NULL_SINK, (reason) => {
      void glanceForChat(reason)
    })
    if (win) client.connect()
  })
  // 后端重启期间会短暂失联，客户端先停掉，重连成功后由 ready 建新的。
  backend.on('lost', () => {
    client?.stop()
    client = null
    notifyTray(cfg.bot.name, '与后端失联，正在重连…')
  })
  backend.on('unavailable', (err) => {
    console.error('[main] 未连接到 Python 后端：', err.message)
    dialog.showErrorBox('未连接到 Python 后端', err.message)
    app.quit()
  })
  backend.start()
  app.on('before-quit', (event) => {
    // 异步幂等退出：首次触发拦截退出，等清理完成后再真正退出；重入时（quitPrepared
    // 已置位）直接放行。所有等待都有内部超时，不会卡住退出。
    if (quitPrepared) return
    quitPrepared = true
    event.preventDefault()
    void (async () => {
      // 由后端拉起时本外壳是整个应用唯一的可见入口，「退出」应当连后端一起结束；
      // 连的是用户自己起的后端时只断开连接——关掉别人的后端不是退出桌宠该有的效果。
      if (managedByBackend && client) {
        await client.shutdownBackend().catch((error: unknown) => {
          console.error('[main] 请求后端退出失败：', error)
        })
      }
      client?.stop()
      backend?.stop()
      inputActivity?.stop()
      disposeWindowCapture()
      app.quit()
    })()
  })

  /**
   * 判断桌宠窗口当前是否可见且仍可操作。
   *
   * @returns {boolean} 窗口实例存在、未销毁且处于可见状态时返回 ``true``。
   */
  const isVisible = (): boolean => !!petWindow && !petWindow.isDestroyed() && petWindow.isVisible()

  // 业务 IPC 直接转发给 Python，不在 Electron 侧维护备用业务状态。
  ipcMain.on(IPC.UserInteracted, () => { /* Python 通过 WebSocket 事件流接收活跃状态。 */ })
  ipcMain.handle(IPC.Send, async (_e, text: string) => {
    if (!client) return
    // 【关键】仅在输入命中屏幕意图或托盘持续截图开关开启时采集画面。
    //
    // 原因：普通聊天不需要上传屏幕内容；按需采集可同时降低延迟、请求成本和隐私暴露面。
    // 当前处理：用户明确请求时同步等待一次采集，后端设置独立超时，失败只影响视觉上下文。
    if (watchScreen || mentionsScreen(text)) await glanceForChat()
    return client.send(text)
  })
  ipcMain.on(IPC.Interrupt, () => client?.interrupt())
  ipcMain.handle(IPC.Diary, async () => {
    if (client) return client.diary()
    return { entries: [], memories: [], now: Date.now() }
  })
  // 前台进程轮询后交给 Python 分类；Electron 保留读取系统前台窗口所需的平台权限。
  let lastTitle = ''
  /**
   * 读取一次前台窗口和输入活动快照，并发送给 Python 后端。
   *
   * @returns {Promise<void>} 发送完成或当前没有可用客户端/配置时完成。
   * @throws 不向轮询调度器传播平台读取和网络异常；异常会被记录并等待下一次轮询。
   * @remarks 过滤桌宠自身窗口，避免把应用自身活动误判为用户正在使用的前台程序；
   *   轮询间隔由 ``FOREGROUND_POLL_MS`` 控制。
   */
  const pollForeground = async (): Promise<void> => {
    if (!client || !currentCfg?.desktop_pet.enabled) return
    try {
      const fg = await readForeground(currentCfg.vision.fullscreen_silent)
      if (!fg || isSelfProcess(fg.process)) return
      await client.foreground({
        process: fg.process,
        title: fg.title ?? '',
        fullscreen: fg.fullscreen ?? false,
        visible: isVisible(),
        input: {
          ...inputActivity?.drain(),
          idleSeconds: powerMonitor.getSystemIdleTime(),
          spanMs: FOREGROUND_POLL_MS,
        },
      })
      lastTitle = fg.title ?? ''
    } catch { /* 前台窗口读取可能因系统权限失败；保留上一状态并等待下一轮。 */ }
  }
  const fgTimer = setInterval(pollForeground, FOREGROUND_POLL_MS)
  fgTimer.unref()
  app.on('before-quit', () => clearInterval(fgTimer))

  // 视觉行为读取模块级 currentCfg；后端重启刷新该引用后，Electron 侧无需重启进程即可应用新开关。
  currentCfg = cfg

  const capturePageUrl = devUrl ? new URL('capture.html', devUrl).href : APP_CAPTURE_URL
  /**
   * 按当前视觉配置选择前台窗口或主屏截图。
   *
   * @param title 前台窗口标题；整屏模式下可为空字符串。
   * @returns 捕获结果；未能取得画面时返回 `null`。
   * @throws 传播底层捕获实现抛出的异常，由调用方统一隔离。
   */
  const captureByMode = async (title: string) => (
    currentCfg?.vision.capture_mode === 'screen'
      ? captureScreen(capturePageUrl)
      : captureWindow(title, capturePageUrl)
  )

  // 屏幕感知仅由显式请求或持续截图开关触发，避免后台定时采集产生过期描述和额外上传。
  // 采集失败、超时或未得到画面都直接放行，视觉能力不能成为文本消息的单点故障。
  /**
   * 按需采集当前前台画面并发送给 Python 对话客户端。
   *
   * @param reason 触发原因，默认值为 `user_request`，用于诊断日志。
   * @returns 采集流程完成后的 Promise；未启用视觉、客户端不存在或捕获失败时正常结束。
   * @throws 不向上抛出截图和上传异常；方法记录错误后结束，以隔离视觉能力故障。
   * @remarks 采集前刷新一次前台窗口标题，防止轮询延迟导致窗口匹配失败；整屏模式不要求标题。
   */
  const glanceForChat = async (reason: string = 'user_request') => {
    if (!currentCfg?.desktop_pet.enabled || !currentCfg.vision.enabled || !client) {
      console.debug('[vision] 截图请求跳过：', {
        reason,
        petEnabled: !!currentCfg?.desktop_pet.enabled,
        visionEnabled: !!currentCfg?.vision.enabled, hasClient: !!client,
      })
      return
    }
    // 先刷新前台标题，避免轮询间隔内标题变化导致 desktopCapturer 无法匹配目标窗口。
    // 当前前台是桌宠自身时保留上一标题，以继续捕获用户与桌宠交互前正在查看的窗口。
    let title = lastTitle
    try {
      const fg = await readForeground(currentCfg.vision.fullscreen_silent)
      if (fg && !isSelfProcess(fg.process) && fg.title) title = fg.title
    } catch { /* 前台读取失败时沿用上一轮标题。 */ }
    // 整屏模式不依赖窗口标题；窗口模式缺少标题时无法安全定位捕获目标。
    if (!title && currentCfg.vision.capture_mode !== 'screen') {
      console.debug('[vision] 现抓跳过：还没拿到任何前台窗口标题')
      return
    }
    try {
      const capture = await captureByMode(title)
      if (!capture) {
        console.warn('[vision] 现抓失败：没截到画面', {
          mode: currentCfg.vision.capture_mode, title,
        })
        return
      }
      await client.screenshotChat(capture.jpeg)
    } catch (error) {
      // 视觉采集属于可选能力，失败不得阻断文本消息；保留错误记录以便确认采集是否执行。
      console.warn('[vision] 现抓请求失败：', error)
    }
  }


  // 注册托盘及其窗口、配置和后端控制动作。
  createTray(petWindow, cfg.bot.name, {
    talk: () => {
      const win = petWindow
      if (!win || win.isDestroyed()) return
      if (!win.isVisible()) togglePet(win, true)
      win.focus()
      win.webContents.send(IPC.OpenComposer)
    },
    watchingScreen: () => watchScreen,
    setWatchingScreen: (on: boolean) => { watchScreen = on },
    resetPosition: () => petWindow && resetPetPosition(petWindow),
    openDiary: () =>
      openDiaryWindow({
        url: devUrl ? new URL('diary.html', devUrl).href : APP_DIARY_URL,
        preload: resolveDiaryPreload(),
        botName: cfg.bot.name,
      }),
    openSettings: () =>
      openSettingsWindow({
        url: devUrl ? new URL('settings.html', devUrl).href : APP_SETTINGS_URL,
        preload: resolveSettingsPreload(),
        botName: cfg.bot.name,
      }),
    restartBackend: () => {
      const target = client
      if (!target) return
      void target.restart().catch((error: unknown) => {
        console.error('[main] 重启请求失败：', error)
      })
    },
  })

  petWindow?.on('closed', () => { petWindow = null })

  // 按环境变量决定是否执行 Electron 自检。
  if (SELFTEST && petWindow) runSelfTest(petWindow)
}

// Electron 自检开关。
const SELFTEST = process.env.YUELI_SELFTEST === '1' || process.argv.includes('--selftest')

/**
 * 输出一条结构化自检结果，并按环境变量要求追加到结果文件。
 *
 * @param tag 结果类型标识，例如窗口、托盘或渲染检查名称。
 * @param payload 可 JSON 序列化的检查结果对象。
 * @returns 无返回值。
 * @throws Error 当配置的输出文件无法追加写入时抛出。
 */
function report(tag: string, payload: unknown): void {
  const line = `${tag} ${JSON.stringify(payload)}`
  console.log(line)
  const out = process.env.YUELI_SELFTEST_OUT
  if (out) appendFileSync(out, `${line}\n`, { encoding: 'utf8' })
}

/**
 * 执行 Electron 侧的无头窗口与渲染机制自检。
 *
 * @param win 待检查的桌宠窗口。
 * @returns 无返回值；检查结果通过标准输出和可选的自检输出文件报告，完成后退出应用。
 * @throws 不主动抛出；浏览器脚本、窗口探测或报告失败会转换为失败报告后退出。
 * @remarks Python 后端业务链路不在此处验证，由 Python 自检入口单独负责。
 */
function runSelfTest(win: BrowserWindow): void {
  win.webContents.once('did-finish-load', async () => {
    await new Promise((r) => setTimeout(r, 2500))
    try {
      const canvas = await win.webContents.executeJavaScript(`(() => {
        const c = document.getElementById('character')
        const err = document.getElementById('error')
        if (!c) return { ok: false, reason: 'canvas 元素不存在' }
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data
        let opaque = 0
        for (let i = 3; i < d.length; i += 4) if (d[i] > 24) opaque++
        return {
          ok: opaque > 0 && err.style.display !== 'flex',
          canvas: c.width + 'x' + c.height,
          opaquePixels: opaque,
          opaqueRatio: +(opaque / (c.width * c.height) * 100).toFixed(1),
          errorVisible: err.style.display === 'flex',
          bridgeAvailable: typeof window.pet?.setInteractive === 'function',
        }
      })()`)
      report('SELFTEST', canvas)
      report('SELFTEST-WINDOW', probeWindow(win))
      report('SELFTEST-HIT', await probeHitRegions(win))
      report('SELFTEST-TRAY', probeTray(win))
      report('SELFTEST-DIARY', await probeDiary())
    } catch (err) {
      report('SELFTEST', { ok: false, reason: String(err) })
    }
    app.quit()
  })
}

/**
 * 检查桌宠窗口是否可聚焦、置顶且能够完成位置往返移动。
 *
 * @param win 待检查的桌宠窗口。
 * @returns 包含窗口边界、置顶状态和移动结果的自检对象。
 * @throws 传播 Electron 窗口属性读取或移动失败产生的异常。
 */
function probeWindow(win: BrowserWindow) {
  const bounds = win.getBounds()
  const pos = win.getPosition()
  const orig = [...pos]
  win.setPosition(pos[0]! + 10, pos[1]! + 10)
  const moved = win.getPosition()
  win.setPosition(orig[0]!, orig[1]!)
  return {
    ok: win.isFocusable() && moved[0] !== orig[0],
    focusable: win.isFocusable(),
    alwaysOnTop: win.isAlwaysOnTop(),
    bounds,
  }
}

/**
 * 验证点击穿透切换以及消息框、输入栏的命中区域布局。
 *
 * @param win 待检查的桌宠窗口。
 * @returns 包含两个 DOM 区域位置和分离状态的自检对象。
 * @throws 传播渲染器脚本执行失败或等待过程中的异常。
 */
async function probeHitRegions(win: BrowserWindow) {
  await win.webContents.executeJavaScript(`window.pet?.setInteractive(false)`)
  await new Promise((r) => setTimeout(r, 150))
  await win.webContents.executeJavaScript(`window.pet?.setInteractive(true)`)
  const overlays = await win.webContents.executeJavaScript(`(() => {
    const composer = document.getElementById('composer')
    const bubble = document.getElementById('bubble')
    if (!composer || !bubble) return null
    const composerRect = composer.getBoundingClientRect()
    const bubbleRect = bubble.getBoundingClientRect()
    return {
      composer: {
        top: Math.round(composerRect.top),
        left: Math.round(composerRect.left),
        width: Math.round(composerRect.width),
        height: Math.round(composerRect.height),
        nearHead: composerRect.top < window.innerHeight * 0.25,
      },
      bubble: {
        top: Math.round(bubbleRect.top),
        left: Math.round(bubbleRect.left),
        width: Math.round(bubbleRect.width),
        height: Math.round(bubbleRect.height),
        nearHead: bubbleRect.top < window.innerHeight * 0.25,
      },
      separated: bubbleRect.right <= composerRect.left || composerRect.right <= bubbleRect.left,
    }
  })()`)
  return {
    ok: overlays?.composer.nearHead === true && overlays.bubble.nearHead === true && overlays.separated === true,
    note: '点击穿透切换未抛错，消息框和输入栏均位于角色头顶且互不遮挡',
    overlays,
  }
}

/**
 * 检查托盘图标是否已加载并记录桌宠窗口的可见状态。
 *
 * @param win 桌宠窗口。
 * @returns 包含图标状态和窗口可见状态的自检对象。
 */
function probeTray(win: BrowserWindow) {
  return {
    ok: !trayIconEmpty(),
    iconLoaded: !trayIconEmpty(),
    windowVisible: win.isVisible(),
  }
}

/**
 * 打开日记窗口并验证页面桥接、列表节点和窗口生命周期。
 *
 * @returns 包含桥接可用性、窗口状态和列表信息的自检结果。
 * @throws 不主动抛出；加载或脚本失败会转换为失败结果，并在 `finally` 中关闭窗口。
 * @remarks 方法会创建并关闭一个真实的 Electron 日记窗口，运行时间受页面加载和固定等待影响。
 */
async function probeDiary() {
  const devUrl = process.env.ELECTRON_RENDERER_URL
  const url = devUrl ? new URL('diary.html', devUrl).href : APP_DIARY_URL
  const win = openDiaryWindow({
    url,
    preload: resolveDiaryPreload(),
    botName: currentCfg?.bot.name ?? '',
  })
  try {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('日记窗口加载超时')), 10_000)
      win.webContents.once('did-finish-load', () => { clearTimeout(timer); resolve() })
      win.webContents.once('did-fail-load', (_e, code, desc) => {
        clearTimeout(timer); reject(new Error(`加载失败 ${code} ${desc}`))
      })
    })
    await new Promise((r) => setTimeout(r, 1200))
    const dom = await win.webContents.executeJavaScript(`(() => ({
      entries: document.getElementById('list')?.querySelectorAll('.entry').length ?? 0,
      bridgeAvailable: typeof window.diary?.read === 'function',
      windowOpen: true,
    }))()`)
    return { ok: dom.bridgeAvailable === true, windowOpen: diaryWindowOpen(), ...dom }
  } catch (err) {
    return { ok: false, reason: String(err) }
  } finally {
    closeDiaryWindow()
  }
}
