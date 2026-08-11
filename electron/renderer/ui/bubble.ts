/**
 * 流式文本气泡控制模块。
 *
 * 本模块负责将增量文本分段缓存到气泡中，按固定时间片逐步写入正文，
 * 在生成速度超过展示速度时动态增加单次揭示量，并在回合结束后按文本长度
 * 计算自动隐藏时间。错误状态、分段提交和 DOM 可见性也由 {@link Bubble}
 * 统一维护。
 *
 * 模块依赖渲染进程的 DOM、浏览器计时器以及 `HTMLElement.textContent` 的
 * 纯文本写入语义；它不依赖模型、网络或主进程接口，因此调用方只需提供
 * 气泡根元素和正文元素即可完成展示。
 *
 * @module renderer/ui/bubble
 */

const REVEAL_MS = 28
/** 待显字数超过这个值就加速，避免长回复的显示落后于生成。 */
const CATCHUP_AT = 12

export class Bubble {
  private pending = ''
  private shown = ''
  private timer = 0
  private hideTimer = 0
  private lines: string[] = []

  /**
   * 创建气泡控制器。
   *
   * @param {HTMLElement} el 控制显示、错误状态和可见性的气泡根元素。
   * @param {HTMLElement} textEl 接收纯文本内容的元素；文本通过
   * `textContent` 写入，不会被解析为 HTML。
   * @returns {Bubble} 已绑定 DOM 元素且内部文本缓冲为空的气泡控制器。
   * @remarks 构造函数不会注册全局监听器，也不会启动计时器；计时器只在
   * {@link push} 或 {@link finish} 等状态变更后按需创建。调用方必须提供真实
   * `HTMLElement`；构造函数不执行运行时类型校验。
   */
  constructor(
    private readonly el: HTMLElement,
    private readonly textEl: HTMLElement,
  ) {}

  /**
   * 开始一个新的发言分段。
   *
   * @returns {void} 无返回值；当前未显示字符会先归档为上一行。
   * @remarks 该方法会取消已有的自动隐藏计时器、提交当前分段并添加 `show`
   * class，但不会启动字符揭示计时器。
   */
  startLine(): void {
    clearTimeout(this.hideTimer)
    this.flushToLines()
    this.el.classList.add('show')
  }

  /**
   * 将流式文本追加到待显示队列。
   *
   * @param {string} text 当前网络块中的文本片段，可以为空字符串；内容会按
   * 原样进入待显示队列，不执行 HTML 解析或长度校验。
   * @returns {void} 无返回值；没有活动揭示计时器时启动一轮异步揭示。
   * @remarks 方法只追加缓冲并安排计时器，不会立即把完整片段写入 DOM；当队列
   * 积压超过追赶阈值时，后续时间片会一次揭示多个字符。方法依赖 TypeScript
   * 类型检查，不在运行时校验非字符串输入。
   */
  push(text: string): void {
    this.pending += text
    if (!this.timer) this.tick()
  }

  /**
   * 立即显示完整文本并跳过逐字揭示。
   *
   * @param {string} text 要显示的完整文本；允许为空字符串。
   * @param {boolean} isError 是否添加错误样式，默认值为 `false`。
   * @returns {void} 无返回值。
   * @remarks 方法会取消当前揭示和自动隐藏计时器，丢弃尚未展示的分段队列，
   * 并将传入文本作为当前完整分段写入 DOM；调用方必须保证构造函数接收的两个
   * 元素仍处于可用状态。
   */
  showNow(text: string, isError = false): void {
    clearTimeout(this.hideTimer)
    this.stop()
    this.lines = []
    this.shown = text
    this.pending = ''
    this.el.classList.toggle('error', isError)
    this.el.classList.add('show')
    this.render()
  }

  /**
   * 结束当前回合，等待剩余字符展示后按文本长度延时隐藏。
   *
   * @returns {void} 无返回值；隐藏通过异步计时器完成。
   * @remarks 当待显示队列非空时，方法会轮询等待揭示完成；队列清空后才创建
   * 自动清理计时器。重复调用可能重新安排隐藏计时。
   */
  finish(): void {
    this.drainThenHide()
  }

  /**
   * 清除所有文本、样式和计时器。
   *
   * @returns {void} 无返回值。
   * @remarks 方法会取消揭示与自动隐藏计时器，清空已显示、待显示和已归档
   * 分段，并移除 `show` 与 `error` class；调用方必须保证构造函数接收的元素
   * 仍处于可用状态。
   */
  clear(): void {
    clearTimeout(this.hideTimer)
    this.stop()
    this.lines = []
    this.shown = ''
    this.pending = ''
    this.el.classList.remove('show', 'error')
    this.render()
  }

  /**
   * 从待显示队列揭示一批字符，并在仍有内容时继续调度。
   *
   * @returns {void} 无返回值；使用追赶步长限制生成与展示之间的延迟。
   * @remarks 每次调用只创建一个 `REVEAL_MS` 间隔的计时器；回调执行后若仍有
   * 待显示文本，会递归安排下一轮，避免同时运行多个揭示循环。
   */
  private tick(): void {
    this.timer = window.setTimeout(() => {
      this.timer = 0
      if (!this.pending) return
      // 队列积压时增加单次揭示量，避免显示速度长期落后于流式生成。
      const step = this.pending.length > CATCHUP_AT ? Math.ceil(this.pending.length / CATCHUP_AT) : 1
      this.shown += this.pending.slice(0, step)
      this.pending = this.pending.slice(step)
      this.render()
      if (this.pending) this.tick()
    }, REVEAL_MS)
  }

  /**
   * 等待待显示队列清空后安排按文本长度计算的隐藏计时器。
   *
   * @returns {void} 无返回值；队列未清空时继续轮询。
   * @remarks 队列未清空时每隔 `REVEAL_MS * 2` 检查一次，队列清空后按完整文本
   * 长度计算停留时间并创建唯一的自动隐藏计时器。
   */
  private drainThenHide(): void {
    if (this.pending) {
      // 先完成剩余揭示，避免回合结束时截断未显示的字符。
      setTimeout(() => this.drainThenHide(), REVEAL_MS * 2)
      return
    }
    // 停留时间随文本长度增加，给用户足够时间读取长消息。
    const dwell = Math.min(12_000, 2600 + this.fullText().length * 90)
    this.hideTimer = window.setTimeout(() => this.clear(), dwell)
  }

  /**
   * 取消字符揭示计时器，但保留已显示和待显示文本。
   *
   * @returns {void} 无返回值。
   * @remarks 该方法只清理揭示计时器，不修改文本缓冲、DOM 内容或隐藏计时器。
   */
  private stop(): void {
    clearTimeout(this.timer)
    this.timer = 0
  }

  /**
   * 将当前分段提交到行列表并清空当前分段缓冲。
   *
   * @returns {void} 无返回值；空白分段不会写入行列表。
   * @remarks 提交前会去除当前分段首尾空白，并移除错误样式，使新分段从正常
   * 状态开始；该方法不会直接刷新正文 DOM。
   */
  private flushToLines(): void {
    const cur = (this.shown + this.pending).trim()
    if (cur) this.lines.push(cur)
    this.shown = ''
    this.pending = ''
    this.el.classList.remove('error')
  }

  /**
   * 生成行列表与当前分段拼接后的完整展示文本。
   *
   * @returns {string} 过滤空行后以换行符连接的完整文本。
   * @remarks 方法会创建临时数组并遍历当前行列表；行数较多时会产生与文本
   * 行数成正比的短暂内存开销，但不会修改内部缓冲。
   */
  private fullText(): string {
    return [...this.lines, this.shown].filter(Boolean).join('\n')
  }

  /**
   * 使用 `textContent` 刷新气泡正文。
   *
   * @returns {void} 无返回值。
   * @remarks 使用 `textContent` 保持模型输出为纯文本，避免把其中的 HTML
   * 标记当作节点解析；每次调用都会覆盖正文元素当前内容。
   */
  private render(): void {
    this.textEl.textContent = this.fullText()
  }
}
