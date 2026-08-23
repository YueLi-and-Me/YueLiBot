/**
 * 使用立绘素材清单实现角色表情、动作和装扮的局部合成渲染。
 *
 * 本模块依赖 CharacterView 抽象和 shared/sprite-manifest.ts 的素材类型，先绘制
 * 基础立绘，再按矩形区域覆盖眼睛和嘴部图层，供 renderer/main.ts 使用。
 */
import type { CharacterView, CharacterViewOptions, Emotion, Gesture, OutfitItem } from '../types.ts'
import type { SpriteManifest } from '../../../shared/sprite-manifest.ts'

/** 渲染循环的帧间隔上限（毫秒），即 30fps。 */
const FRAME_MS = 1000 / 30

/**
 * 立绘差分实现。
 *
 * 渲染采用局部合成：先绘制当前表情整图，再覆盖眼部和嘴部矩形，使眨眼和口型
 * 不会把表情退回默认脸。Canvas2D 已满足绘图、矩形覆盖和轻微变换需求，不引入
 * 额外 WebGL 运行时。
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
  private lastFrameAt = 0
  private disposed = false

  ready = false

  /**
   * 返回当前生效的表情。
   *
   * @returns {Emotion} 当前立绘正在使用的表情标识。
   */
  get currentEmotion(): Emotion {
    return this.emotion
  }

  /**
   * 创建立绘视图并初始化 Canvas2D 上下文。
   *
   * @param opts Canvas、素材根路径、过渡时长和眨眼间隔配置。
   * @throws Error 浏览器无法创建 2D 上下文时抛出。
   */
  constructor(private readonly opts: CharacterViewOptions) {
    this.canvas = opts.canvas
    const ctx = this.canvas.getContext('2d')
    if (!ctx) throw new Error('拿不到 2D 上下文')
    this.ctx = ctx
    this.transitionMs = opts.transitionMs ?? 150
    this.blinkIntervalMs = opts.blinkIntervalMs ?? 4200
  }

  /**
   * 加载 manifest 和所有必要的立绘资源，并启动渲染循环。
   *
   * @returns 所有资源加载完成且首帧循环已安排后的 Promise。
   * @throws Error manifest 请求失败、JSON 结构不兼容或任一图片加载失败。
   * @sideEffects 更新资源缓存、Canvas 尺寸、ready 状态并创建 requestAnimationFrame
   * 循环；失败时不将视图标记为就绪。
   */
  async load(): Promise<void> {
    const base = this.opts.assetsBase.replace(/\/+$/, '')
    // 先读取清单，再根据实际使用的图层收集文件，避免预加载未引用素材。
    const res = await fetch(`${base}/manifest.json`)
    if (!res.ok) throw new Error(`读不到 manifest.json（HTTP ${res.status}）`)
    const manifest = (await res.json()) as SpriteManifest

    const files = new Set<string>([manifest.base])
    for (const e of Object.values(manifest.expressions)) {
      files.add(e.file)
      if (e.blink) files.add(e.blink)
    }
    for (const f of Object.values(manifest.mouth)) files.add(f)

    // Set 去重后并行加载，既避免同一文件重复请求，也缩短首次可用时间。
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

    // 只有清单和全部图片均可用时才公开 ready，避免渲染循环绘制半套资源。
    this.manifest = manifest
    this.canvas.width = manifest.canvas.width
    this.canvas.height = manifest.canvas.height
    this.ready = true
    this.nextBlinkAt = performance.now() + this.blinkIntervalMs
    this.loop()
  }

  // --- CharacterView ---

  /**
   * 设置目标表情并启动与当前表情之间的交叉淡化。
   *
   * @param emotion 角色词表中的目标表情。
   * @returns 无返回值；与当前表情相同时不创建新过渡。
   * @sideEffects 更新当前和上一表情及过渡开始时间。
   */
  setEmotion(emotion: Emotion): void {
    if (emotion === this.emotion) return
    // 淡出的是当前正显示的整图。连续快速切换时不能把中间态当起点，
    // 否则过渡透明度会重复衰减并产生残影。
    this.prevEmotion = this.emotion
    this.emotion = emotion
    this.transitionStart = performance.now()
  }

  /**
   * 播放一个持续指定时长的动作状态。
   *
   * @param gesture 角色词表中的动作标识。
   * @param durationMs 动作持续毫秒数，默认 1600；非正数会在下一帧清除。
   * @returns 无返回值。
   * @sideEffects 更新动作和到期时间，渲染循环会在到期后清除动作。
   */
  playGesture(gesture: Gesture, durationMs = 1600): void {
    this.gesture = gesture
    this.gestureUntil = performance.now() + durationMs
  }

  /**
   * 设置口型开合度并限制到渲染约定的值域。
   *
   * @param value 期望的开合度，任意数值会被截断到 ``[0, 1]``。
   * @returns 无返回值。
   */
  setMouthOpen(value: number): void {
    this.mouthOpen = Math.max(0, Math.min(1, value))
  }

  /**
   * 接收视线坐标以满足 CharacterView 接口；立绘素材不提供眼球局部变换，因此保持空操作。
   *
   * @param _x 归一化或像素视线横坐标；当前实现不使用。
   * @param _y 归一化或像素视线纵坐标；当前实现不使用。
   * @returns {void} 不修改当前立绘。
   */
  lookAt(_x: number, _y: number): void {}

  /**
   * 记录当前装扮列表，等待素材清单提供对应图层后参与合成。
   *
   * @param items 只读装扮标识列表。
   * @returns 无返回值；当前素材方案仅保存列表，不绘制未提供的装扮图层。
   * @sideEffects 替换内部装扮引用。
   */
  setOutfit(items: readonly OutfitItem[]): void {
    // 当前清单没有装扮图层，先保存状态以便后续素材版本直接复用。
    this.outfit = items
  }

  /**
   * 销毁渲染循环并释放图片缓存。
   *
   * @returns 无返回值；重复调用安全。
   * @sideEffects 取消动画帧、清空图片引用并将 ready 置为 ``false``。
   */
  destroy(): void {
    this.disposed = true
    cancelAnimationFrame(this.raf)
    this.images.clear()
    this.ready = false
  }

  // --- 渲染 ---

  private loop = (): void => {
    if (this.disposed) return
    // 渲染上限 30fps：待机微动（呼吸/摇摆）使每帧都有绘制，rAF 在高刷屏上会
    // 跑到 120/144Hz，整图 drawImage 的 GPU 占用随之翻倍。30fps 是桌宠类应用的
    // 常见帧率，肉眼无感知差异，渲染开销直接减半以上。
    const now = performance.now()
    if (now - this.lastFrameAt >= FRAME_MS) {
      this.lastFrameAt = now
      this.draw(now)
    }
    this.raf = requestAnimationFrame(this.loop)
  }

  /**
   * 根据表情获取已缓存的主立绘图片。
   *
   * @param emotion 目标表情。
   * @returns 对应图片；未加载清单时返回 ``null``，缺失表情时回退到 normal/base。
   */
  private imageFor(emotion: Emotion): HTMLImageElement | null {
    const m = this.manifest
    if (!m) return null
    const entry = m.expressions[emotion] ?? m.expressions.normal
    return entry ? (this.images.get(entry.file) ?? null) : this.images.get(m.base) ?? null
  }

  /**
   * 获取指定表情的闭眼覆盖图。
   *
   * @param emotion 当前表情。
   * @returns 闭眼图片；清单未定义闭眼图或资源未加载时返回 ``null``。
   */
  private blinkImageFor(emotion: Emotion): HTMLImageElement | null {
    const entry = this.manifest?.expressions[emotion]
    return entry?.blink ? (this.images.get(entry.blink) ?? null) : null
  }

  /**
   * 按时间戳绘制当前立绘、过渡、待机微动、眨眼和口型图层。
   *
   * @param now 当前 performance 时间戳。
   * @returns 无返回值；清单未加载时不绘制。
   * @sideEffects 修改 Canvas2D 状态、更新动作/眨眼状态并绘制当前帧。
   */
  private draw(now: number): void {
    const m = this.manifest
    if (!m) return

    const { width, height } = m.canvas
    this.ctx.clearRect(0, 0, width, height)

    this.updateBlink(now)
    if (this.gesture && now > this.gestureUntil) this.gesture = null

    // 待机微动：呼吸缩放 + 极轻微的左右摇摆。
    // 幅度必须保持在低范围，避免待机微动覆盖角色主体动作语义。
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
   *
   * @returns {void} 无返回值；清单未提供眼区、眨眼阶段为零或资源未加载时直接返回。
   * @remarks 仅修改当前 Canvas2D 绘制状态，不改变 manifest 或图片缓存。
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

  /**
   * 将当前口型映射为离散嘴型并覆盖到脸部区域。
   *
   * @returns {void} 无返回值；清单、嘴区或目标图片缺失时直接返回。
   * @remarks 使用 ``closed``、``half``、``open`` 三档阈值，避免连续插值削弱嘴型状态的可辨识度。
   */
  private compositeMouth(): void {
    const m = this.manifest
    const r = m?.regions?.mouth
    if (!m || !r) return

    // 三档映射使用阈值而不是插值，避免连续形变削弱嘴型状态的可辨识度。
    const id = this.mouthOpen > 0.55 ? 'open' : this.mouthOpen > 0.18 ? 'half' : 'closed'
    if (id === 'closed') return // 闭嘴态就是表情图本身，不用盖

    const file = m.mouth[id]
    const img = file ? this.images.get(file) : null
    if (!img) return

    this.ctx.drawImage(img, r.x, r.y, r.width, r.height, r.x, r.y, r.width, r.height)
  }

  /**
   * 根据时间戳更新随机眨眼动画的闭合阶段。
   *
   * @param now 当前 performance 时间戳，单位为毫秒。
   * @returns {void} 眨眼间隔不为正时直接返回。
   * @sideEffects 更新眨眼阶段、动画起止时间和下一次触发时间。
   */
  private updateBlink(now: number): void {
    if (this.blinkIntervalMs <= 0) return

    const DURATION = 120

    if (this.blinkStart) {
      const p = (now - this.blinkStart) / DURATION
      if (p >= 1) {
        this.blinkStart = 0
        this.blinkPhase = 0
        // 在基础间隔上加入 ±40% 抖动，避免固定节拍造成机械化观感。
        this.nextBlinkAt = now + this.blinkIntervalMs * (0.6 + Math.random() * 0.8)
      } else {
        // 使用三角波使闭合和睁开阶段保持对称。
        this.blinkPhase = p < 0.5 ? p * 2 : (1 - p) * 2
      }
      return
    }

    if (now >= this.nextBlinkAt) this.blinkStart = now
  }
}
