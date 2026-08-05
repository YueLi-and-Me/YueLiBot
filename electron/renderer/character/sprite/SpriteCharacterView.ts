import type { CharacterView, CharacterViewOptions, Emotion, Gesture, OutfitItem } from '../types.ts'
import type { SpriteManifest } from '../../../shared/sprite-manifest.ts'

/**
 * AI 立绘差分实现。
 *
 * 核心是**局部合成**而非整图切换：
 * 先画当前表情的整图，再把眼部/嘴部矩形单独覆盖上去。
 * 如果眨眼和口型也走整图切换，她一开口表情就会退回平静脸 ——
 * 因为嘴型差分是基于 normal 生成的。
 *
 * 用 Canvas2D 而不是 PixiJS：这里要的只是画图、盖矩形、加点变换，
 * 2D 上下文全都有，没必要为此背一个 WebGL 引擎。Live2D 那份才需要。
 */
export class SpriteCharacterView implements CharacterView {
  private readonly ctx: CanvasRenderingContext2D
  private readonly canvas: HTMLCanvasElement
  private readonly transitionMs: number
  private readonly blinkIntervalMs: number

  private manifest: SpriteManifest | null = null
  private images = new Map<string, HTMLImageElement>()

  /** 当前生效的表情，和正在淡出的上一个表情。 */
  private emotion: Emotion = 'normal'
  private prevEmotion: Emotion | null = null
  private transitionStart = 0

  private gesture: Gesture | null = null
  private gestureUntil = 0

  private mouthOpen = 0
  private blinkPhase = 0 // 0 = 睁眼，1 = 全闭
  private nextBlinkAt = 0
  private blinkStart = 0

  private outfit: readonly OutfitItem[] = []
  private raf = 0
  private startedAt = performance.now()
  private disposed = false

  ready = false

  /** 当前生效的表情。供自检读取 —— 否则「说完话脸有没有松回去」只能靠肉眼。 */
  get currentEmotion(): Emotion {
    return this.emotion
  }

  constructor(private readonly opts: CharacterViewOptions) {
    this.canvas = opts.canvas
    const ctx = this.canvas.getContext('2d')
    if (!ctx) throw new Error('拿不到 2D 上下文')
    this.ctx = ctx
    this.transitionMs = opts.transitionMs ?? 150
    this.blinkIntervalMs = opts.blinkIntervalMs ?? 4200
  }

  async load(): Promise<void> {
    const base = this.opts.assetsBase.replace(/\/+$/, '')
    const res = await fetch(`${base}/manifest.json`)
    if (!res.ok) throw new Error(`读不到 manifest.json（HTTP ${res.status}）`)
    const manifest = (await res.json()) as SpriteManifest

    const files = new Set<string>([manifest.base])
    for (const e of Object.values(manifest.expressions)) {
      files.add(e.file)
      if (e.blink) files.add(e.blink)
    }
    for (const f of Object.values(manifest.mouth)) files.add(f)

    await Promise.all(
      [...files].map(
        (f) =>
          new Promise<void>((resolve, reject) => {
            const img = new Image()
            img.onload = () => {
              this.images.set(f, img)
              resolve()
            }
            img.onerror = () => reject(new Error(`图片加载失败：${f}`))
            img.src = `${base}/${f}`
          }),
      ),
    )

    this.manifest = manifest
    this.canvas.width = manifest.canvas.width
    this.canvas.height = manifest.canvas.height
    this.ready = true
    this.nextBlinkAt = performance.now() + this.blinkIntervalMs
    this.loop()
  }

  // --- CharacterView ---

  setEmotion(emotion: Emotion): void {
    if (emotion === this.emotion) return
    // 淡出的是「当前正显示的那张」。连续快速切换时不要把中间态当起点，
    // 否则会出现越切越淡的鬼影
    this.prevEmotion = this.emotion
    this.emotion = emotion
    this.transitionStart = performance.now()
  }

  playGesture(gesture: Gesture, durationMs = 1600): void {
    this.gesture = gesture
    this.gestureUntil = performance.now() + durationMs
  }

  setMouthOpen(value: number): void {
    this.mouthOpen = Math.max(0, Math.min(1, value))
  }

  /** 立绘没有眼球参数，按接口契约降级为空操作。 */
  lookAt(_x: number, _y: number): void {}

  setOutfit(items: readonly OutfitItem[]): void {
    // 立绘方案下装扮需要单独出图，当前素材集不含，先记录不渲染
    this.outfit = items
  }

  destroy(): void {
    this.disposed = true
    cancelAnimationFrame(this.raf)
    this.images.clear()
    this.ready = false
  }

  // --- 渲染 ---

  private loop = (): void => {
    if (this.disposed) return
    this.draw(performance.now())
    this.raf = requestAnimationFrame(this.loop)
  }

  private imageFor(emotion: Emotion): HTMLImageElement | null {
    const m = this.manifest
    if (!m) return null
    const entry = m.expressions[emotion] ?? m.expressions.normal
    return entry ? (this.images.get(entry.file) ?? null) : this.images.get(m.base) ?? null
  }

  private blinkImageFor(emotion: Emotion): HTMLImageElement | null {
    const entry = this.manifest?.expressions[emotion]
    return entry?.blink ? (this.images.get(entry.blink) ?? null) : null
  }

  private draw(now: number): void {
    const m = this.manifest
    if (!m) return

    const { width, height } = m.canvas
    this.ctx.clearRect(0, 0, width, height)

    this.updateBlink(now)
    if (this.gesture && now > this.gestureUntil) this.gesture = null

    // 待机微动：呼吸缩放 + 极轻微的左右摇摆。
    // 幅度必须小 —— 大了就从「活着」变成「抽搐」
    const t = (now - this.startedAt) / 1000
    const breath = 1 + Math.sin(t * 1.15) * 0.006
    const sway = Math.sin(t * 0.47) * 0.9

    this.ctx.save()
    this.ctx.translate(width / 2 + sway, height)
    this.ctx.scale(breath, breath)
    this.ctx.translate(-width / 2, -height)

    const fade = this.transitionMs > 0 ? Math.min(1, (now - this.transitionStart) / this.transitionMs) : 1

    if (this.prevEmotion && fade < 1) {
      const prev = this.imageFor(this.prevEmotion)
      if (prev) {
        this.ctx.globalAlpha = 1 - fade
        this.ctx.drawImage(prev, 0, 0)
      }
    } else if (this.prevEmotion) {
      this.prevEmotion = null
    }

    const cur = this.imageFor(this.emotion)
    if (cur) {
      this.ctx.globalAlpha = this.prevEmotion ? fade : 1
      this.ctx.drawImage(cur, 0, 0)
    }
    this.ctx.globalAlpha = 1

    this.compositeEyes()
    this.compositeMouth()

    this.ctx.restore()
  }

  /**
   * 眨眼：把闭眼图的眼部矩形盖到当前脸上。
   * 只覆盖矩形而非整图，这样任何表情都能眨眼，不会退回 normal 脸。
   */
  private compositeEyes(): void {
    const r = this.manifest?.regions?.eyes
    if (!r || this.blinkPhase <= 0.01) return

    const closed = this.blinkImageFor(this.emotion)
    if (!closed) return

    this.ctx.globalAlpha = this.blinkPhase
    this.ctx.drawImage(closed, r.x, r.y, r.width, r.height, r.x, r.y, r.width, r.height)
    this.ctx.globalAlpha = 1
  }

  /** 口型：同理，只换嘴部矩形。 */
  private compositeMouth(): void {
    const m = this.manifest
    const r = m?.regions?.mouth
    if (!m || !r) return

    // 三档映射。用阈值而不是插值 —— 真人说话嘴型本来就是跳变的，
    // 平滑过渡反而像在嚼东西
    const id = this.mouthOpen > 0.55 ? 'open' : this.mouthOpen > 0.18 ? 'half' : 'closed'
    if (id === 'closed') return // 闭嘴态就是表情图本身，不用盖

    const file = m.mouth[id]
    const img = file ? this.images.get(file) : null
    if (!img) return

    this.ctx.drawImage(img, r.x, r.y, r.width, r.height, r.x, r.y, r.width, r.height)
  }

  /** 眨眼节拍：随机间隔 + 一次约 120ms 的闭合-睁开。 */
  private updateBlink(now: number): void {
    if (this.blinkIntervalMs <= 0) return

    const DURATION = 120

    if (this.blinkStart) {
      const p = (now - this.blinkStart) / DURATION
      if (p >= 1) {
        this.blinkStart = 0
        this.blinkPhase = 0
        // ±40% 抖动，规律眨眼看着像机器人
        this.nextBlinkAt = now + this.blinkIntervalMs * (0.6 + Math.random() * 0.8)
      } else {
        // 三角波：闭到底再睁开
        this.blinkPhase = p < 0.5 ? p * 2 : (1 - p) * 2
      }
      return
    }

    if (now >= this.nextBlinkAt) this.blinkStart = now
  }
}
