import type { CharacterView } from '../character/types.ts'

/**
 * 语音播放与口型驱动。
 *
 * 口型不靠猜文字长度，而是**实测音频响度**：把播放中的音频接进
 * AnalyserNode，每帧取 RMS 驱动 setMouthOpen。这样停顿、气声、拖长音
 * 都会自然反映到嘴上 —— 按字数估算的假口型一眼就能看出对不上。
 */

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
  /** 播放队列。她可能一次说两三句，必须串行播，不能叠在一起。 */
  private queue: Array<{ data: ArrayBuffer; format: string }> = []
  private playing = false

  constructor(private readonly view: CharacterView) {}

  /** 首次播放时才建 AudioContext —— 没有用户手势时创建会被浏览器挂起。 */
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

  enqueue(data: ArrayBuffer, format = 'mp3'): void {
    this.queue.push({ data, format })
    if (!this.playing) void this.drain()
  }

  /** 打断：立刻停声、清队列、闭嘴。 */
  stop(): void {
    this.queue.length = 0
    try {
      this.source?.stop()
    } catch {
      /* 已经停了 */
    }
    this.source = null
    this.playing = false
    this.stopPump()
    this.level = 0
    this.view.setMouthOpen(0)
  }

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

  private async playOne(data: ArrayBuffer): Promise<void> {
    const ctx = this.ensureContext()
    if (ctx.state === 'suspended') await ctx.resume().catch(() => {})

    let audio: AudioBuffer
    try {
      // decodeAudioData 会转移 ArrayBuffer 的所有权，传副本以便重试
      audio = await ctx.decodeAudioData(data.slice(0))
    } catch {
      // 解码失败（格式不对、字节被截断）不该卡住整个队列
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
        // 必须连内部电平一起归零，不能只把嘴设成 0：
        // level 留在上一句的高位的话，下一句会从那个值开始往下滑，
        // 开头几百毫秒嘴是错误张开的
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
   * 用定时器而不是 requestAnimationFrame：**音频在页面不可见时不会暂停，
   * 而 rAF 会停**。用 rAF 的话，她说话中途被收进托盘再拿出来，
   * 嘴会僵在上一帧的开度上，跟正在播的声音完全脱钩。
   * 口型只是一次赋值，按 50Hz 跑的开销可以忽略。
   */
  private startPump(): void {
    if (this.raf) return
    this.raf = window.setInterval(this.pump, 20)
  }

  private stopPump(): void {
    if (this.raf) clearInterval(this.raf)
    this.raf = 0
  }

  /** 每帧取一次 RMS 驱动嘴型。 */
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
   * 是否正在出声。
   *
   * 渲染层用它决定要不要启用「打字节奏近似口型」——
   * 两套驱动同时写 setMouthOpen 会让嘴抽搐。
   */
  get active(): boolean {
    return this.playing || this.queue.length > 0
  }

  /** 供自检：当前嘴张开度。 */
  get currentLevel(): number {
    return this.level
  }
}

/** base64 → ArrayBuffer。IPC 传的是 base64，见 shared/ipc.ts 的说明。 */
export function decodeBase64(b64: string): ArrayBuffer {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes.buffer
}
