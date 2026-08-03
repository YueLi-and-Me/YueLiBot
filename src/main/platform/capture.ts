import {
  BrowserWindow,
  desktopCapturer,
  type DesktopCapturerSource,
  session,
  type Session,
} from 'electron'

/**
 * 屏幕捕获。
 *
 * ★ platform 适配层。也是整个项目**隐私风险最高**的一个文件 ——
 *   它拿到的东西比别处都多，所以约束全部写在这里，不散到调用方。
 *
 * 三条硬规矩：
 *  1. **只截前台那一个窗口**，不截整个桌面 —— 全屏截会捎上第二屏、
 *     后台的聊天窗、没关的银行页面
 *  2. **绝不落盘**。返回 Buffer，用完即弃；不进缓存、不进日记、不进记忆
 *  3. **截完就缩**。视觉模型看 768px 宽足够判断「发生了什么」，
 *     原分辨率既慢又贵，还平白多传一堆能认出细节的像素
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
 * 截取前台窗口。
 *
 * `desktopCapturer` 不告诉我们哪个是前台，只能拿窗口标题去匹配 ——
 * 所以调用方要把 active-win 读到的标题传进来。
 * 匹配不上时返回 null 而不是退回全屏截：**宁可不看，也不要多看**。
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
    // 捕获失败不能拖垮桌宠，但必须留下原始原因，不能把系统或权限问题静默吞掉。
    console.warn('[capture] 前台窗口截图失败：', error)
    return null
  } finally {
    selectedSource = null
    captureInProgress = false
  }
}

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

/** 主进程退出前释放隐藏捕获页和它的临时会话处理器。 */
export function disposeWindowCapture(): void {
  selectedSource = null
  captureSession?.setDisplayMediaRequestHandler(null)
  captureSession = null
  if (captureRenderer && !captureRenderer.isDestroyed()) captureRenderer.destroy()
  captureRenderer = null
}

/**
 * 画面变化程度（0~1）。
 *
 * 用来避免「没事也一直调用视觉模型」—— 那既烧钱又慢。
 * 死亡画面、结算界面这类值得开口的时刻，视觉上都很突兀，
 * 正好能被这种粗粒度的变化量抓住。
 *
 * 刻意做得很糙：JPEG 字节流的分块均值。不需要精确，只需要
 * 「有没有大变化」这一个比特的信息，而精确的感知哈希要多引一个库。
 */
export function frameDelta(a: Buffer | null, b: Buffer): number {
  if (!a || !a.length || !b.length) return 1

  const BUCKETS = 64
  const mean = (buf: Buffer): number[] => {
    const out = new Array<number>(BUCKETS).fill(0)
    const step = Math.max(1, Math.floor(buf.length / BUCKETS))
    for (let i = 0; i < BUCKETS; i++) {
      let sum = 0
      const start = i * step
      const end = Math.min(buf.length, start + step)
      for (let j = start; j < end; j += 7) sum += buf[j]!
      out[i] = sum / Math.max(1, Math.ceil((end - start) / 7))
    }
    return out
  }

  const ma = mean(a)
  const mb = mean(b)
  let diff = 0
  for (let i = 0; i < BUCKETS; i++) diff += Math.abs(ma[i]! - mb[i]!)
  // 归一到 0~1。除数是经验值：整屏换画面大约落在 0.3 以上
  return Math.min(1, diff / (BUCKETS * 40))
}
