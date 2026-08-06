/**
 * Electron 主进程 —— 纯平台层。
 *
 * 业务逻辑（记忆、人格、日程、LLM）全部搬进 Python 后端。
 * 这里只剩：窗口管理、托盘、平台权限调用、前台进程轮询、截图捕获。
 *
 * 两侧边界：
 *   · 渲染层：只通过 preload bridge 通信，IPC 形状不变（electron/shared/ipc.ts）
 *   · Python：通过 PythonSupervisor/PythonClient（HTTP + WS on 127.0.0.1）
 */

import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs'
import { basename, join } from 'node:path'
import { app, BrowserWindow, ipcMain, powerMonitor, screen } from 'electron'
import 'dotenv/config'

import {
  APP_CAPTURE_URL, APP_DIARY_URL, APP_INDEX_URL, APP_OBSERVABILITY_URL, APP_SETTINGS_URL,
  registerAppScheme, serveAppScheme,
} from './platform/appProtocol.ts'
import { closeDiaryWindow, diaryWindowOpen, openDiaryWindow } from './platform/diaryWindow.ts'
import { foregroundAvailable, isSelfProcess, readForeground } from './platform/foreground.ts'
import { captureScreen, captureWindow, disposeWindowCapture } from './platform/capture.ts'
import {
  beginDrag, createPetWindow, endDrag, focusForInput,
  resolveDiaryPreload, resolveObservabilityPreload, resolvePreload,
  resolveRendererRoot, resolveSettingsPreload,
  setInteractive,
} from './platform/petWindow.ts'
import { closeObservabilityWindow, observabilityWindowOpen, openObservabilityWindow } from './platform/observabilityWindow.ts'
import { closeSettingsWindow, openSettingsWindow } from './platform/settingsWindow.ts'
import {
  createTray, destroyTray, notifyTray, resetPetPosition, togglePet, trayIconEmpty,
} from './platform/tray.ts'
import {
  assertConfigConsistent, configIsComplete, ensureNapcatConfig, readConfigDirectory,
  tryPrefillFromLegacyEnv, writeConfigDirectory,
} from './config.ts'
import { IPC, type YueliConfig } from '../shared/ipc.ts'
import { mentionsScreen } from './screenIntent.ts'
import { InputActivity } from './inputActivity.ts'
import { PythonSupervisor } from './python/supervisor.ts'
import { PythonClient, windowSink } from './python/client.ts'
import { resolveRuntimePaths } from './runtimePaths.ts'

const PET_W = 440
const PET_H = 480

/** 前台轮询间隔（毫秒）。比原来的 ProactiveGate 稍快，Python 侧有自己的节流。 */
const FOREGROUND_POLL_MS = 8_000

if (process.env.YUELI_DISABLE_GPU === '1') app.disableHardwareAcceleration()

const runtimePaths = resolveRuntimePaths(app.getAppPath(), process.env.YUELI_PROJECT_ROOT)
for (const directory of [
  runtimePaths.dataDir,
  runtimePaths.electronUserDataDir,
  runtimePaths.electronSessionDataDir,
  runtimePaths.electronTempDir,
  runtimePaths.electronCrashDumpsDir,
]) {
  mkdirSync(directory, { recursive: true })
}
// 必须在 app.ready 之前改写 Electron 的路径，否则 Chromium 会先在 C 盘 AppData
// 建 Cache、Local Storage、崩溃转储和临时文件。
app.setPath('userData', runtimePaths.electronUserDataDir)
app.setPath('sessionData', runtimePaths.electronSessionDataDir)
app.setPath('temp', runtimePaths.electronTempDir)
app.setPath('crashDumps', runtimePaths.electronCrashDumpsDir)

let petWindow: BrowserWindow | null = null
let supervisor: PythonSupervisor | null = null
let client: PythonClient | null = null
/** 启动后一直保持"当前有效配置"的引用，点"重启月璃"时刷新——
 * 决定 Electron 这一侧的行为（比如要不要轮询截图）不能只看启动那一刻的值。 */
let currentCfg: YueliConfig | null = null
let inputActivity: InputActivity | null = null
/**
 * 托盘「让她看着屏幕」开关。开着＝每条消息都截一帧；关着＝只在他问起屏幕时
 * 才截（见 screenIntent.ts）。只存在于本次运行，重启回到关——持续截屏是个
 * 应该每次主动开启的动作，不该悄悄地跨会话生效。
 */
let watchScreen = false
registerAppScheme()

function syncInputActivity(): void {
  if (!inputActivity) return
  if (currentCfg?.generation.proactive.enabled) {
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
  const napcatConfigPath = ensureNapcatConfig(configDir)

  // ── 配置读写 IPC（设置窗口首次启动和后续编辑共用）───────────────────
  ipcMain.handle(IPC.ReadConfig, async () => readConfigDirectory(configDir, legacyConfigPath))
  ipcMain.handle(IPC.SaveConfig, async (_e, config: YueliConfig) => {
    try {
      // 结构自检只卡用户编辑这一条路径：迁移写回的是刚读进来的旧配置，
      // 用同一把尺子量会让老用户升级后直接起不来。
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
    if (!supervisor) return
    console.log('[main] 收到重启指令，重启 Python 后端…')
    currentCfg = readConfigDirectory(configDir, legacyConfigPath)
    syncInputActivity()
    supervisor.stop()
    supervisor.start()
  })
  ipcMain.on(IPC.OpenSettings, () => {
    openSettingsWindow({
      url: devUrl ? new URL('settings.html', devUrl).href : APP_SETTINGS_URL,
      preload: resolveSettingsPreload(),
    })
  })

  // ── 首次启动：模型/API Key 没填就先弹设置窗口，桌宠和 Python 都先不起 ──
  if (!configIsComplete(readConfigDirectory(configDir, legacyConfigPath))) {
    await runFirstRunWizard(devUrl)
  }

  await startApp(devUrl, readConfigDirectory(configDir, legacyConfigPath), napcatConfigPath)
})

/** 老用户从仓库根目录的 .env 迁移；prefill 直接写进拆分配置目录，
 * 这样设置窗口的 read() 首次读到的就是这些值，不用另开一条 IPC 通道传初值。 */
function runFirstRunWizard(devUrl?: string): Promise<void> {
  const legacyEnvPath = join(app.getAppPath(), '.env')
  const prefill = tryPrefillFromLegacyEnv(legacyEnvPath)
  if (prefill) {
    const base = readConfigDirectory(configDir, legacyConfigPath)
    writeConfigDirectory(configDir, { ...base, ...prefill })
  }

  return new Promise<void>((resolve) => {
    firstRunResolve = resolve
    openSettingsWindow({
      url: devUrl
        ? new URL('settings.html?mode=first-run', devUrl).href
        : `${APP_SETTINGS_URL}?mode=first-run`,
      preload: resolveSettingsPreload(),
    })
  }).then(() => {
    closeSettingsWindow()
  })
}

async function startApp(
  devUrl: string | undefined,
  cfg: YueliConfig,
  napcatConfigPath: string,
): Promise<void> {
  currentCfg = cfg
  inputActivity = new InputActivity()
  syncInputActivity()
  petWindow = createPetWindow({
    width: PET_W, height: PET_H,
    url: devUrl ?? APP_INDEX_URL,
    preload: resolvePreload(),
  })

  // ── 窗口控制 IPC ───────────────────────────────────────────────────
  ipcMain.on(IPC.SetInteractive, (_e, interactive: boolean) => {
    if (petWindow) setInteractive(petWindow, interactive)
  })
  ipcMain.on(IPC.Quit, () => app.quit())
  ipcMain.on(IPC.BeginDrag, () => petWindow && beginDrag(petWindow))
  ipcMain.on(IPC.EndDrag, () => endDrag())
  ipcMain.on(IPC.FocusInput, (_e, focus: boolean) => petWindow && focusForInput(petWindow, focus))

  // ── Python 后端监护 ─────────────────────────────────────────────────
  supervisor = new PythonSupervisor({
    dataDir,
    configPath: configDir,
    cwd: app.getAppPath(),
    pythonExe: process.env.YUELI_PYTHON_EXE ?? 'python',
    napcatConfigPath,
  })
  supervisor.on('ready', (port) => {
    if (!petWindow || petWindow.isDestroyed() || !supervisor) return
    client?.stop()
    client = new PythonClient(port, supervisor.token, windowSink(petWindow), (reason) => {
      void glanceForChat(reason)
    })
    client.connect()
  })
  supervisor.on('failed', (err) => {
    console.warn('[supervisor] Python 后端不可用：', err.message)
  })
  supervisor.on('adapterFailed', (err) => {
    console.warn('[supervisor] QQ 适配器不可用：', err.message)
    notifyTray(err.message)
  })
  supervisor.start()
  app.on('before-quit', () => {
    supervisor?.stop()
    client?.stop()
    inputActivity?.stop()
    disposeWindowCapture()
  })

  const isVisible = () => !!petWindow && !petWindow.isDestroyed() && petWindow.isVisible()

  // ── 业务 IPC（全部转发给 Python，无降级）──────────────────────────
  ipcMain.on(IPC.UserInteracted, () => { /* Python 侧通过 WS 事件流感知活跃 */ })
  ipcMain.handle(IPC.Send, async (_e, text: string) => {
    if (!client) return 0
    // ★ 只有他真的问起屏幕（或自己把托盘开关打开）时才截。闲聊时一张图都不截，
    //   零延迟零费用，画面也不出本机。是他主动问的，那就同步等——Python 侧
    //   有 8 秒截止线兜底，超时她会如实说看不清。
    if (watchScreen || mentionsScreen(text)) await glanceForChat()
    return client.send(text)
  })
  ipcMain.on(IPC.Interrupt, () => client?.interrupt())
  ipcMain.handle(IPC.Diary, async () => {
    if (client) return client.diary()
    return { entries: [], memories: [], now: Date.now() }
  })
  ipcMain.handle(IPC.Observability, async () => {
    if (client) return client.observability()
    return {}
  })
  ipcMain.handle(IPC.DebugTrace, async (_e, since: number) => {
    if (client) return client.debugTrace(since ?? 0)
    return []
  })

  // ── 前台进程轮询 → Python ───────────────────────────────────────────
  // Electron 保持对前台窗口的读取特权；Python 侧做分类和感知判断。
  let lastTitle = ''
  const pollForeground = async () => {
    if (!client || !currentCfg) return
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
    } catch { /* 前台读取可能因权限失败，静默 */ }
  }
  const fgTimer = setInterval(pollForeground, FOREGROUND_POLL_MS)
  fgTimer.unref()
  app.on('before-quit', () => clearInterval(fgTimer))

  // ★ 开关读的是模块级 currentCfg（点"重启月璃"时会刷新，见顶部那个 handler），
  //   不是启动时捕获的常量——否则在设置窗口里打开视觉功能、点重启，
  //   Electron 这一侧永远不会跟着生效，得整个应用重启才行，
  //   和"重启月璃"这个按钮承诺的效果对不上。
  currentCfg = cfg

  const capturePageUrl = devUrl ? new URL('capture.html', devUrl).href : APP_CAPTURE_URL
  /** 按配置决定截前台窗口还是整屏。截整屏时不需要窗口标题。 */
  const captureByMode = async (title: string) => (
    currentCfg?.vision.capture_mode === 'screen'
      ? captureScreen(capturePageUrl)
      : captureWindow(title, capturePageUrl)
  )

  // ── 屏幕感知：只在他问起时跑 ────────────────────────────────────────
  // ★ 曾经还有一条每 12s 的后台轮询链路做「全时态感知」。它和现抓两条路加起来
  //   有九个互相牵制的时间常量横跨两种语言，排错要同时记住九个数字，实际表现
  //   却是她拿几分钟前的旧描述当现在讲。整条删掉了：他不问，就不看。
  //   于是这里可以放心同步等——是他主动问的，等几秒天经地义。
  // 失败、超时、没截到都直接放行，视觉不能成为聊天的单点故障。
  const glanceForChat = async (reason: string = 'user_request') => {
    if (!currentCfg?.vision.enabled || !client) {
      console.debug('[vision] 截图请求跳过：', {
        reason,
        visionEnabled: !!currentCfg?.vision.enabled, hasClient: !!client,
      })
      return
    }
    // ★ 先刷新一次前台标题再截，不要直接用 lastTitle：前台轮询最长有 8s 延迟，
    //   而终端、浏览器这类窗口标题一直在变，标题一漂 desktopCapturer 就匹配不上，
    //   截图整个失败。实测就是这么丢掉一次现抓的（lastTitle 停在一个已经改掉的
    //   终端标题上）。当前前台是桌宠自己时保留 lastTitle——那正是他转头跟她说话
    //   之前在看的窗口。
    let title = lastTitle
    try {
      const fg = await readForeground(currentCfg.vision.fullscreen_silent)
      if (fg && !isSelfProcess(fg.process) && fg.title) title = fg.title
    } catch { /* 前台读取失败就沿用 lastTitle */ }
    // 截整屏时不需要标题，标题为空也照样能截。
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
      // 视觉是锦上添花，失败不能拖累发消息——但必须留痕，否则没法诊断
      // "现抓到底有没有跑"。
      console.warn('[vision] 现抓请求失败：', error)
    }
  }


  // ── 托盘 ────────────────────────────────────────────────────────────
  createTray(petWindow, {
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
      }),
    openObservability: () =>
      openObservabilityWindow({
        url: devUrl ? new URL('observability.html', devUrl).href : APP_OBSERVABILITY_URL,
        preload: resolveObservabilityPreload(),
      }),
    openSettings: () =>
      openSettingsWindow({
        url: devUrl ? new URL('settings.html', devUrl).href : APP_SETTINGS_URL,
        preload: resolveSettingsPreload(),
      }),
    restartBackend: () => { supervisor?.stop(); supervisor?.start() },
  })

  petWindow.on('closed', () => { petWindow = null })

  // ── 自检 ─────────────────────────────────────────────────────────────
  if (SELFTEST) runSelfTest(petWindow)
}

// ── 自检常量 ───────────────────────────────────────────────────────────
const SELFTEST = process.env.YUELI_SELFTEST === '1' || process.argv.includes('--selftest')

function report(tag: string, payload: unknown): void {
  const line = `${tag} ${JSON.stringify(payload)}`
  console.log(line)
  const out = process.env.YUELI_SELFTEST_OUT
  if (out) appendFileSync(out, `${line}\n`, { encoding: 'utf8' })
}

/**
 * 无头自检 —— 只验 Electron 侧的窗口和渲染机制。
 * SELFTEST-CHAT / REFLECT / AWARE 的逻辑已移至 Python CLI 自检（python bot.py --selftest）。
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

function probeTray(win: BrowserWindow) {
  return {
    ok: !trayIconEmpty(),
    iconLoaded: !trayIconEmpty(),
    windowVisible: win.isVisible(),
  }
}

async function probeDiary() {
  const devUrl = process.env.ELECTRON_RENDERER_URL
  const url = devUrl ? new URL('diary.html', devUrl).href : APP_DIARY_URL
  const win = openDiaryWindow({ url, preload: resolveDiaryPreload() })
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
