/**
 * 管理浏览器音频播放、缓存和基于响度的角色口型驱动。
 *
 * 音频资源来自主进程转发的语音事件，角色视图由 character/types.ts 注入；本模块
 * 在播放期间读取 AnalyserNode 的 RMS 值，不根据文本长度猜测口型。
 */
import type { CharacterView } from '../character/types.ts'

/** 语音播放使用 AnalyserNode 的 RMS 驱动角色口型，不依赖文本长度估算。 */

/** RMS 到嘴张开度的映射。人声 RMS 常在 0.02~0.25，直接用会几乎不张嘴。 */
const GAIN = 6
/** 低于此视为静音，避免底噪让嘴一直微张。 */
const NOISE_FLOOR = 0.012
/**
 * 平滑系数。太小嘴会抖成筛子，太大又跟不上音节。
 * 张嘴比闭嘴快 —— 真人开口是骤然的，闭合是滑落的。
 */
const ATTACK = 0.5
const RELEASE = 0.18

export class VoicePlayer {
  private ctx: AudioContext | null = null
  private analyser: AnalyserNode | null = null
  private source: AudioBufferSourceNode | null = null
  // 显式标注底层是 ArrayBuffer：Uint8Array 默认参数是 ArrayBufferLike，
  // 而 getByteTimeDomainData 只接受非共享内存的视图
  private buf: Uint8Array<ArrayBuffer> = new Uint8Array(0)
  /** 口型驱动定时器句柄。 */
  private raf = 0
  private level = 0
  /** 播放队列；语音片段必须串行播放，避免多个 AudioBufferSource 重叠。 */
  private queue: Array<{ data: ArrayBuffer; format: string }> = []
  private playing = false

  /**
   * 创建语音播放器。
   *
   * @param view 接收嘴型开合度的角色视图；值域为 ``[0, 1]``。
   */
  constructor(private readonly view: CharacterView) {}

  /**
   * 延迟创建音频上下文和分析器。
   *
   * @returns {AudioContext} 已创建或复用的音频上下文。
   * @sideEffects 首次调用时创建 AudioContext、AnalyserNode 和 RMS 缓冲区，并连接到默认输出。
   */
  private ensureContext(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext()
      this.analyser = this.ctx.createAnalyser()
      // 2048 在响应速度和稳定性之间比较平衡；再小 RMS 会跳，再大会迟钝
      this.analyser.fftSize = 2048
      this.analyser.connect(this.ctx.destination)
      this.buf = new Uint8Array(this.analyser.fftSize)
    }
    return this.ctx
  }

  /**
   * 将一段音频加入播放队列。
   *
   * @param data 已解码前的音频二进制数据。
   * @param format 音频格式标识，默认 ``mp3``；当前解码器由浏览器自动判断。
   * @returns 无返回值；无播放任务时立即异步开始消费队列。
   * @sideEffects 增加队列长度并可能创建 AudioContext、启动音频播放和口型采样。
   */
  enqueue(data: ArrayBuffer, format = 'mp3'): void {
    this.queue.push({ data, format })
    if (!this.playing) void this.drain()
  }

  /**
   * 立即停止当前音频并清空剩余队列。
   *
   * @returns 无返回值；底层 source 已结束时同样安全。
   * @sideEffects 停止音频源、取消口型定时器并将角色嘴型恢复为 0。
   */
  stop(): void {
    this.queue.length = 0
    try {
      this.source?.stop()
    } catch {
      /* 底层音频源已结束，忽略重复停止错误。 */
    }
    this.source = null
    this.playing = false
    this.stopPump()
    this.level = 0
    this.view.setMouthOpen(0)
  }

  /**
   * 按入队顺序串行播放所有音频片段。
   *
   * @returns 队列清空且当前播放状态复位后的 Promise。
   * @sideEffects 逐段调用解码器和音频源；无论中间是否解码失败，最终都会关闭口型。
   */
  private async drain(): Promise<void> {
    this.playing = true
    try {
      while (this.queue.length) {
        const next = this.queue.shift()!
        await this.playOne(next.data)
      }
    } finally {
      this.playing = false
      this.view.setMouthOpen(0)
    }
  }

  /**
   * 解码并播放单段音频，同时启动和停止口型采样。
   *
   * @param data 单段音频的 ArrayBuffer。
   * @returns 音频结束或解码失败后的 Promise。
   * @sideEffects 可能恢复 AudioContext、创建 AudioBufferSourceNode、更新角色嘴型。
   */
  private async playOne(data: ArrayBuffer): Promise<void> {
    const ctx = this.ensureContext()
    if (ctx.state === 'suspended') await ctx.resume().catch(() => {})

    let audio: AudioBuffer
    try {
      // decodeAudioData 会转移 ArrayBuffer 的所有权，传副本以便重试
      audio = await ctx.decodeAudioData(data.slice(0))
    } catch {
      // 解码失败只丢弃当前片段，不能阻塞后续已排队的语音。
      return
    }

    return new Promise<void>((resolve) => {
      const src = ctx.createBufferSource()
      src.buffer = audio
      src.connect(this.analyser!)
      this.source = src

      src.onended = () => {
        if (this.source === src) this.source = null
        this.stopPump()
        // 同时清理内部电平，避免下一段从上一段的高值开始平滑，造成开头口型残留。
        this.level = 0
        this.view.setMouthOpen(0)
        resolve()
      }

      src.start()
      this.startPump()
    })
  }

  /**
   * 启停口型驱动。
   *
   * 使用定时器而不是 requestAnimationFrame：音频在页面不可见时仍继续播放，
   * 口型采样不能因窗口隐藏而停止。采样仅执行数值赋值，50Hz 的运行开销可控。
   *
   * @returns {void} 已存在采样定时器时直接返回，否则创建 20 毫秒间隔的定时器。
   * @remarks 方法只允许存在一个采样定时器，避免多个定时器同时驱动同一角色视图。
   */
  private startPump(): void {
    if (this.raf) return
    this.raf = window.setInterval(this.pump, 20)
  }

  /**
   * 停止口型采样定时器。
   *
   * @returns 无返回值；定时器不存在时安全返回。
   * @sideEffects 清除 setInterval 并重置定时器句柄。
   */
  private stopPump(): void {
    if (this.raf) clearInterval(this.raf)
    this.raf = 0
  }

  /**
   * 读取当前音频帧的 RMS 并更新角色嘴型。
   *
   * @returns {void} 无分析器或无活动音频源时直接返回。
   * @sideEffects 读取 AnalyserNode 时域数据、更新平滑电平并调用角色视图的嘴型接口。
   */
  private pump = (): void => {
    const analyser = this.analyser
    if (!analyser || !this.source) return

    analyser.getByteTimeDomainData(this.buf)
    let sum = 0
    for (let i = 0; i < this.buf.length; i++) {
      // 时域数据以 128 为零点，归一化到 -1~1
      const v = (this.buf[i]! - 128) / 128
      sum += v * v
    }
    const rms = Math.sqrt(sum / this.buf.length)
    const target = rms < NOISE_FLOOR ? 0 : Math.min(1, rms * GAIN)

    // 张嘴快、闭嘴慢 —— 真人开口是骤然的，闭合是滑落的
    const k = target > this.level ? ATTACK : RELEASE
    this.level += (target - this.level) * k
    this.view.setMouthOpen(this.level)
  }

  /**
   * 返回播放器是否有当前音频或待播放音频。
   *
   * @returns {boolean} 正在播放或队列中仍有音频片段时返回 ``true``。
   * 渲染层据此避免同时启用打字节奏口型；两套驱动同时写入会产生竞争。
   */
  get active(): boolean {
    return this.playing || this.queue.length > 0
  }

  /**
   * 返回当前口型平滑电平。
   *
   * @returns {number} 当前嘴部开合度，范围为 ``[0, 1]``。
   */
  get currentLevel(): number {
    return this.level
  }
}

/**
 * 将 IPC 传输的 base64 音频数据解码为 ArrayBuffer。
 *
 * @param b64 合法 base64 文本；空字符串返回长度为 0 的 ArrayBuffer。
 * @returns {ArrayBuffer} 按原始字节顺序还原的音频缓冲区。
 * @throws DOMException 输入包含非法 base64 字符时由 atob 抛出。
 * @sideEffects 仅在内存中分配解码缓冲区，不写入文件或修改全局状态。
 */
export function decodeBase64(b64: string): ArrayBuffer {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes.buffer
}
