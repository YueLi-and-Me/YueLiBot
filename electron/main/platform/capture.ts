/**
 * 捕获桌面或前台窗口的 JPEG 图像，并按配置限制尺寸和质量。
 *
 * 本模块封装 Electron desktopCapturer、屏幕尺寸计算及会话权限检查，返回给
 * 主进程后由 Python 客户端按需提交视觉接口；原始图像不由本模块持久化。
 */
import {
  BrowserWindow,
  desktopCapturer,
  type DesktopCapturerSource,
  screen,
  session,
  type Session,
} from 'electron'

/**
 * 定义屏幕捕获适配层的安全边界和资源生命周期。
 *
 * 默认只捕获前台窗口；只有 `vision.capture_mode = 'screen'` 时才捕获主屏。所有结果
 * 都在内存中缩放为不超过 768×432 的 JPEG，调用方使用后不由本模块持久化。隐藏渲染器
 * 通过独立的 Electron session 处理 `getDisplayMedia` 权限，主进程退出时由
 * {@link disposeWindowCapture} 释放。
 */

/** 送进视觉模型的宽度。再大对「画面在发生什么」的判断没有增益。 */
const CAPTURE_WIDTH = 768
const CAPTURE_HEIGHT = 432
const CAPTURE_PARTITION = 'yueli-window-capture'

interface CapturedFrame {
  dataUrl: string
  width: number
  height: number
}

let captureRenderer: BrowserWindow | null = null
let captureSession: Session | null = null
let selectedSource: DesktopCapturerSource | null = null
let captureInProgress = false

/**
 * 真正取帧的代码在隔离渲染页里执行。
 *
 * 先用 0×0 缩略图枚举来源，再把命中的 source 交给 getDisplayMedia，
 * 这样 Chromium 只会为目标窗口启动一个 WGC 会话，不会为了生成来源列表
 * 去尝试捕获所有后台窗口。
 */
const CAPTURE_FRAME_SCRIPT = `
(async () => {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    audio: false,
    video: {
      width: { ideal: ${CAPTURE_WIDTH} },
      height: { ideal: ${CAPTURE_HEIGHT} },
      frameRate: { ideal: 1, max: 1 },
    },
  })

  try {
    const video = document.querySelector('video')
    const canvas = document.querySelector('canvas')
    if (!video || !canvas) throw new Error('捕获页缺少 video 或 canvas 元素')

    const track = stream.getVideoTracks()[0]
    if (!track) throw new Error('前台窗口捕获流没有视频轨道')

    // capture.html 的 video 带 autoplay。必须在设置 srcObject 之前监听首帧，
    // 否则静态窗口可能已经呈现了唯一一帧，随后注册的“下一帧”回调会一直等到超时。
    const frameWaiter = (() => {
      const readyEvents = ['loadeddata', 'canplay', 'resize']
      let callbackId = null
      let timer = null
      let settled = false
      let cancel = () => {}

      const promise = new Promise((resolve, reject) => {
        const cleanup = () => {
          readyEvents.forEach((eventName) => video.removeEventListener(eventName, resolveIfReady))
          video.removeEventListener('error', rejectMediaError)
          if (callbackId !== null) video.cancelVideoFrameCallback(callbackId)
          if (timer !== null) clearTimeout(timer)
        }
        const settle = (callback, value) => {
          if (settled) return
          settled = true
          cleanup()
          callback(value)
        }
        const hasCurrentFrame = () =>
          video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
          video.videoWidth > 0 &&
          video.videoHeight > 0
        function resolveIfReady() {
          if (hasCurrentFrame()) settle(resolve)
        }
        function rejectMediaError() {
          const code = video.error?.code ?? 'unknown'
          const message = video.error?.message ?? '未知媒体错误'
          settle(reject, new Error('前台窗口视频流错误：code=' + code + ', message=' + message))
        }

        readyEvents.forEach((eventName) => video.addEventListener(eventName, resolveIfReady))
        video.addEventListener('error', rejectMediaError)
        if (typeof video.requestVideoFrameCallback === 'function') {
          callbackId = video.requestVideoFrameCallback(resolveIfReady)
        }
        timer = setTimeout(() => {
          const settings = track.getSettings()
          settle(reject, new Error(
            '等待前台窗口视频帧超时：readyState=' + video.readyState +
            ', videoSize=' + video.videoWidth + 'x' + video.videoHeight +
            ', trackState=' + track.readyState +
            ', trackMuted=' + track.muted +
            ', trackSettings=' + JSON.stringify(settings),
          ))
        }, 5_000)
        cancel = () => settle(resolve)
        resolveIfReady()
      })

      return { promise, cancel }
    })()

    video.srcObject = stream
    try {
      await Promise.all([video.play(), frameWaiter.promise])
    } catch (error) {
      frameWaiter.cancel()
      throw error
    }

    const scale = Math.min(1, ${CAPTURE_WIDTH} / video.videoWidth, ${CAPTURE_HEIGHT} / video.videoHeight)
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
    const context = canvas.getContext('2d')
    if (!context) throw new Error('无法创建捕获画布上下文')
    context.drawImage(video, 0, 0, canvas.width, canvas.height)

    return {
      dataUrl: canvas.toDataURL('image/jpeg', 0.72),
      width: canvas.width,
      height: canvas.height,
    }
  } finally {
    stream.getTracks().forEach((track) => track.stop())
  }
})()
`

export interface Capture {
  jpeg: Buffer
  /** 缩略图尺寸，供变化检测复用。 */
  width: number
  height: number
  /** 截的是哪个窗口（仅用于本地日志，不外传）。 */
  sourceName: string
}

/**
 * 按窗口标题截取前台窗口的缩略图。
 *
 * @param title 前台窗口标题；空值或空白字符串表示放弃本次捕获。
 * @param capturePageUrl 隐藏捕获渲染器加载的页面地址。
 * @returns 成功时返回内存中的 JPEG、尺寸和本地窗口名；标题未匹配、已有捕获任务或捕获失败时返回 `null`。
 * @throws 不向上抛出 desktopCapturer、渲染脚本或图像解码异常；方法记录错误后返回 `null`。
 * @remarks 方法使用精确匹配优先、双向包含匹配兜底，绝不因窗口匹配失败自动扩大为整屏捕获。
 */
export async function captureWindow(title: string | undefined, capturePageUrl: string): Promise<Capture | null> {
  if (!title?.trim() || captureInProgress) return null
  captureInProgress = true

  try {
    // 这里只取窗口元数据。宽高设为 0 是 Electron 官方提供的“不要生成缩略图”方式，
    // 避免 Chromium 在筛选目标之前就尝试捕获每一个后台窗口。
    const sources = await desktopCapturer.getSources({
      types: ['window'],
      thumbnailSize: { width: 0, height: 0 },
      fetchWindowIcons: false,
    })

    // 标题可能被系统截断或带上后缀，用双向包含放宽一点，但不做模糊匹配
    const want = title.trim()
    const hit = sources.find((s) => s.name === want) ?? sources.find((s) => s.name.includes(want) || want.includes(s.name))
    if (!hit) return null

    const renderer = await ensureCaptureRenderer(capturePageUrl)
    selectedSource = hit
    const frame = await renderer.webContents.executeJavaScript(CAPTURE_FRAME_SCRIPT, true) as CapturedFrame
    const prefix = 'data:image/jpeg;base64,'
    if (!frame.dataUrl.startsWith(prefix)) throw new Error('捕获页返回了无效的 JPEG 数据')
    const jpeg = Buffer.from(frame.dataUrl.slice(prefix.length), 'base64')
    if (!jpeg.length) throw new Error('捕获页返回了空 JPEG')

    return {
      jpeg,
      width: frame.width,
      height: frame.height,
      sourceName: hit.name,
    }
  } catch (error) {
    // 捕获失败不得阻断桌宠流程，同时保留原始原因，避免系统或权限错误失去诊断信息。
    console.warn('[capture] 前台窗口截图失败：', error)
    return null
  } finally {
    selectedSource = null
    captureInProgress = false
  }
}

/**
 * 截取主显示器的屏幕缩略图。
 *
 * @param capturePageUrl 隐藏捕获渲染器加载的页面地址。
 * @returns 成功时返回主屏 JPEG、尺寸和来源名称；没有屏幕来源、已有捕获任务或捕获失败时返回 `null`。
 * @throws 不向上抛出屏幕枚举、渲染脚本或图像解码异常；方法记录错误后返回 `null`。
 * @remarks 该方法会把桌面、任务栏和可见窗口一并纳入图像，只在明确选择整屏模式时由调用方触发。
 */
export async function captureScreen(capturePageUrl: string): Promise<Capture | null> {
  if (captureInProgress) return null
  captureInProgress = true

  try {
    const sources = await desktopCapturer.getSources({
      types: ['screen'],
      thumbnailSize: { width: 0, height: 0 },
      fetchWindowIcons: false,
    })
    if (!sources.length) return null

    // display_id 与 Electron 的 Display.id 对得上，用它锁定主屏；
    // 匹配不上时退回第一块（单显示器场景下两者本来就是同一个）。
    const primaryId = String(screen.getPrimaryDisplay().id)
    const hit = sources.find((s) => s.display_id === primaryId) ?? sources[0]!

    const renderer = await ensureCaptureRenderer(capturePageUrl)
    selectedSource = hit
    const frame = await renderer.webContents.executeJavaScript(CAPTURE_FRAME_SCRIPT, true) as CapturedFrame
    const prefix = 'data:image/jpeg;base64,'
    if (!frame.dataUrl.startsWith(prefix)) throw new Error('捕获页返回了无效的 JPEG 数据')
    const jpeg = Buffer.from(frame.dataUrl.slice(prefix.length), 'base64')
    if (!jpeg.length) throw new Error('捕获页返回了空 JPEG')

    return { jpeg, width: frame.width, height: frame.height, sourceName: hit.name }
  } catch (error) {
    console.warn('[capture] 整屏截图失败：', error)
    return null
  } finally {
    selectedSource = null
    captureInProgress = false
  }
}

/**
 * 创建或复用隐藏的离屏捕获渲染器及其独立会话。
 *
 * @param capturePageUrl 捕获页面地址。
 * @returns 已加载捕获页面的隐藏 BrowserWindow。
 * @throws Error 当页面加载失败时抛出；失败路径会销毁窗口并清理 session 处理器。
 * @remarks 首次调用创建离屏窗口，后续捕获复用同一窗口以减少渲染器和权限初始化开销。
 */
async function ensureCaptureRenderer(capturePageUrl: string): Promise<BrowserWindow> {
  if (captureRenderer && !captureRenderer.isDestroyed()) return captureRenderer

  captureSession = session.fromPartition(CAPTURE_PARTITION, { cache: false })
  captureSession.setDisplayMediaRequestHandler((_request, callback) => {
    callback(selectedSource ? { video: selectedSource } : {})
  })

  const renderer = new BrowserWindow({
    show: false,
    webPreferences: {
      backgroundThrottling: false,
      contextIsolation: true,
      nodeIntegration: false,
      offscreen: true,
      session: captureSession,
    },
  })
  renderer.on('closed', () => {
    if (captureRenderer === renderer) captureRenderer = null
  })

  try {
    await renderer.loadURL(capturePageUrl)
    captureRenderer = renderer
    return renderer
  } catch (error) {
    renderer.destroy()
    captureSession.setDisplayMediaRequestHandler(null)
    captureSession = null
    throw error
  }
}

/**
 * 释放隐藏捕获页、媒体请求处理器和当前来源引用。
 *
 * @returns 无返回值；重复调用安全。
 * @remarks 主进程退出及捕获渲染器异常关闭时使用，释放后下一次捕获会重新创建会话。
 */
export function disposeWindowCapture(): void {
  selectedSource = null
  captureSession?.setDisplayMediaRequestHandler(null)
  captureSession = null
  if (captureRenderer && !captureRenderer.isDestroyed()) captureRenderer.destroy()
  captureRenderer = null
}
