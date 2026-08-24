/**
 * WebUI 前端性能探针：定位「打开/切换页面后整机卡顿」的取证工具。
 *
 * 启用方式（默认零开销，未启用时只做一次查询参数判断后立即返回）：
 * - URL 带 `?perf=1` 启用，并写入 localStorage 使后续会话保持启用；
 * - URL 带 `?perf=0` 停用并清除标记。
 *
 * 采集维度（1Hz 采样，环形缓冲保留最近 10 分钟）：
 * - FPS 与最大帧间隔：区分「主线程忙」与「合成器/GPU 忙」；
 * - Long Tasks（>50ms）：主线程被 JS/布局阻塞的直接证据；
 * - JS 堆内存（Chrome `performance.memory`）：持续增长指向内存泄漏；
 * - DOM 节点数（5s 一次）：节点失控增长的证据；
 * - WebSocket 消息速率与字节量（按连接 URL 分组）：日志/事件流风暴；
 * - fetch 请求速率：轮询失控；
 * - 页面生命周期与未捕获错误：切换标签页、freeze/resume、JS 异常风暴。
 *
 * 导出方式：
 * - 页面还活着：控制台执行 `__perf.download()` 下载 JSON，或 `__perf.dump()`
 *   取字符串；
 * - 整机卡死重启后：采样每 10 秒持久化到 localStorage，重新打开页面执行
 *   `__perf.previous()` 读取卡死前最后一次存档。
 */
interface WsChannelStats {
  /** 采样窗口内消息条数。 */
  messages: number
  /** 采样窗口内消息字节总量。 */
  bytes: number
}

/** 单个采样点。 */
interface PerfSample {
  /** Unix 毫秒时间戳。 */
  t: number
  /** 过去 1 秒的帧数。 */
  fps: number
  /** 过去 1 秒最大帧间隔（毫秒）；远大于 100 即肉眼卡顿。 */
  gap: number
  /** 过去 1 秒 long task 条数。 */
  longTasks: number
  /** 过去 1 秒最长 long task 时长（毫秒）。 */
  worstTask: number
  /** JS 堆已用字节；浏览器不支持时为 0。 */
  heap: number
  /** DOM 节点总数（每 5 秒刷新一次，其余采样点复用旧值）。 */
  dom: number
  /** 各 WebSocket 连接的消息速率，键为连接 URL。 */
  ws: Record<string, WsChannelStats>
  /** 过去 1 秒 fetch 请求数。 */
  fetches: number
  /** 该采样点附带的标记事件（切换标签页、异常等），无则为空数组。 */
  marks: string[]
}

/** 探针在 window 上暴露的控制台接口。 */
export interface PerfProbeHandle {
  /** 返回环形缓冲全部采样的 JSON 字符串。 */
  dump: () => string
  /** 触发浏览器下载采样 JSON 文件。 */
  download: () => void
  /** 读取上一次会话持久化的采样（卡死重启后取证用）。 */
  previous: () => string
}

declare global {
  interface Window {
    __perf?: PerfProbeHandle
  }
}

const STORAGE_ENABLE_KEY = 'yueli-perf-enabled'
const STORAGE_TRACE_KEY = 'yueli-perf-trace'
/** 环形缓冲容量：1Hz × 600 = 最近 10 分钟。 */
const MAX_SAMPLES = 600
/** localStorage 持久化间隔：每 10 个采样写一次，兼顾 IO 开销与取证时效。 */
const PERSIST_EVERY = 10

/**
 * 判断并规范化探针启用状态。
 *
 * @returns 当前会话是否启用探针。
 */
function resolveEnabled(): boolean {
  const params = new URLSearchParams(location.search)
  const flag = params.get('perf')
  if (flag === '1') {
    localStorage.setItem(STORAGE_ENABLE_KEY, '1')
    return true
  }
  if (flag === '0') {
    localStorage.removeItem(STORAGE_ENABLE_KEY)
    return false
  }
  return localStorage.getItem(STORAGE_ENABLE_KEY) === '1'
}

/**
 * 初始化性能探针；未启用时立即返回，不产生任何运行开销。
 *
 * @remarks 必须在创建任何 WebSocket / 发起 fetch 之前调用（main.tsx 中
 * 先于 createRoot），否则消息速率统计会漏掉最早建立的连接。
 */
export function initPerfProbe(): void {
  if (!resolveEnabled()) return

  const samples: PerfSample[] = []
  const marks: string[] = []
  const wsStats = new Map<string, WsChannelStats>()
  let frameCount = 0
  let worstGap = 0
  let lastFrameAt = performance.now()
  let longTaskCount = 0
  let worstTask = 0
  let fetchCount = 0
  let domCount = 0

  /* ---- WebSocket 速率统计：子类化原生构造器，按连接 URL 分桶 ---- */
  const NativeWebSocket = window.WebSocket
  class ProbeWebSocket extends NativeWebSocket {
    constructor(url: string | URL, protocols?: string | string[]) {
      super(url, protocols)
      const key = String(url)
      if (!wsStats.has(key)) wsStats.set(key, { messages: 0, bytes: 0 })
      this.addEventListener('message', (event) => {
        const bucket = wsStats.get(key)
        if (!bucket) return
        bucket.messages += 1
        const data = event.data as unknown
        bucket.bytes += typeof data === 'string' ? data.length : 0
      })
      this.addEventListener('close', () => marks.push(`ws-close ${key}`))
      this.addEventListener('error', () => marks.push(`ws-error ${key}`))
    }
  }
  window.WebSocket = ProbeWebSocket as typeof WebSocket

  /* ---- fetch 速率统计 ---- */
  const nativeFetch = window.fetch.bind(window)
  window.fetch = ((...args: Parameters<typeof fetch>) => {
    fetchCount += 1
    return nativeFetch(...args)
  }) as typeof fetch

  /* ---- Long Task 观察 ---- */
  if (typeof PerformanceObserver !== 'undefined') {
    try {
      const observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          longTaskCount += 1
          if (entry.duration > worstTask) worstTask = entry.duration
          if (entry.duration > 500) {
            marks.push(`longtask ${Math.round(entry.duration)}ms`)
          }
        }
      })
      observer.observe({ entryTypes: ['longtask'] })
    } catch {
      // 浏览器不支持 longtask 类型时静默降级，其余维度照常工作。
    }
  }

  /* ---- FPS 与帧间隔 ---- */
  const tick = (now: number) => {
    frameCount += 1
    const gap = now - lastFrameAt
    if (gap > worstGap) worstGap = gap
    lastFrameAt = now
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)

  /* ---- 生命周期与异常标记 ---- */
  const mark = (name: string) => () => marks.push(name)
  document.addEventListener('visibilitychange', () => {
    marks.push(document.hidden ? 'hidden' : 'visible')
  })
  window.addEventListener('freeze', mark('freeze'))
  window.addEventListener('resume', mark('resume'))
  window.addEventListener('pagehide', mark('pagehide'))
  window.addEventListener('error', (event) => marks.push(`error ${event.message}`))
  window.addEventListener('unhandledrejection', () => marks.push('unhandledrejection'))

  /* ---- HUD：右下角单行状态条，1Hz 用 textContent 刷新，成本可忽略 ---- */
  const hud = document.createElement('div')
  hud.style.cssText =
    'position:fixed;right:8px;bottom:8px;z-index:2147483647;padding:4px 8px;' +
    'font:11px/1.5 ui-monospace,monospace;color:#e6ebf6;background:rgba(20,24,38,.88);' +
    'border-radius:6px;pointer-events:none;white-space:pre'
  document.body.appendChild(hud)

  const takeSample = () => {
    const heap =
      (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory
        ?.usedJSHeapSize ?? 0
    const ws: Record<string, WsChannelStats> = {}
    for (const [url, bucket] of wsStats) {
      ws[url] = { ...bucket }
      bucket.messages = 0
      bucket.bytes = 0
    }
    const sample: PerfSample = {
      t: Date.now(),
      fps: frameCount,
      gap: Math.round(worstGap),
      longTasks: longTaskCount,
      worstTask: Math.round(worstTask),
      heap,
      dom: domCount,
      ws,
      fetches: fetchCount,
      marks: marks.splice(0, marks.length),
    }
    samples.push(sample)
    if (samples.length > MAX_SAMPLES) samples.shift()
    if (samples.length % PERSIST_EVERY === 0) {
      try {
        localStorage.setItem(STORAGE_TRACE_KEY, JSON.stringify(samples))
      } catch {
        // 存储配额满时丢弃持久化，内存缓冲不受影响。
      }
    }

    frameCount = 0
    worstGap = 0
    longTaskCount = 0
    worstTask = 0
    fetchCount = 0

    const wsTotal = Object.values(sample.ws).reduce((sum, bucket) => sum + bucket.messages, 0)
    const hot = sample.fps < 24 || sample.worstTask > 300
    hud.style.color = hot ? '#ff9db1' : '#e6ebf6'
    hud.textContent =
      `fps ${sample.fps}  gap ${sample.gap}ms  task ${sample.worstTask}ms×${sample.longTasks}  ` +
      `heap ${(sample.heap / 1048576).toFixed(0)}MB  dom ${sample.dom}  ws ${wsTotal}/s  fetch ${sample.fetches}/s`
  }

  /* DOM 节点数 5 秒刷一次，全量计数本身有成本不宜每秒做 */
  const countDom = () => {
    domCount = document.getElementsByTagName('*').length
  }
  countDom()

  setInterval(takeSample, 1_000)
  setInterval(countDom, 5_000)

  window.__perf = {
    dump: () => JSON.stringify(samples),
    download: () => {
      const blob = new Blob([JSON.stringify(samples, null, 2)], { type: 'application/json' })
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `webui-perf-${new Date().toISOString().replace(/[:.]/g, '-')}.json`
      link.click()
      URL.revokeObjectURL(link.href)
    },
    previous: () => localStorage.getItem(STORAGE_TRACE_KEY) ?? '（没有上一会话的存档）',
  }

  console.info('[perf-probe] 已启用：__perf.dump() / __perf.download() / __perf.previous()')
}
