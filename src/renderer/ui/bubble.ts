/**
 * 对话气泡。
 *
 * 打字机不是「给每个字加延迟」那么简单：模型本来就是流式吐字的，
 * 再叠一层固定延迟会让显示越落后于生成，长句子结束好几秒后字还在爬。
 * 这里用追赶式揭示 —— 待显队列越长揭示越快，既平滑又不脱节。
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

  constructor(
    private readonly el: HTMLElement,
    private readonly textEl: HTMLElement,
  ) {}

  /** 开一段新发言。第二段起会另起一行，而不是覆盖上一段。 */
  startLine(): void {
    clearTimeout(this.hideTimer)
    this.flushToLines()
    this.el.classList.add('show')
  }

  push(text: string): void {
    this.pending += text
    if (!this.timer) this.tick()
  }

  /** 立刻显示完整文本，不再逐字。用于报错或用户主动跳过。 */
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

  /** 本轮结束：把残留字符吐完，然后延时淡出。 */
  finish(): void {
    this.drainThenHide()
  }

  clear(): void {
    clearTimeout(this.hideTimer)
    this.stop()
    this.lines = []
    this.shown = ''
    this.pending = ''
    this.el.classList.remove('show', 'error')
    this.render()
  }

  private tick(): void {
    this.timer = window.setTimeout(() => {
      this.timer = 0
      if (!this.pending) return
      // 队列积压时一次多吐几个字追上生成速度
      const step = this.pending.length > CATCHUP_AT ? Math.ceil(this.pending.length / CATCHUP_AT) : 1
      this.shown += this.pending.slice(0, step)
      this.pending = this.pending.slice(step)
      this.render()
      if (this.pending) this.tick()
    }, REVEAL_MS)
  }

  private drainThenHide(): void {
    if (this.pending) {
      // 还有字没显示完就等它显示完，别把话截断
      setTimeout(() => this.drainThenHide(), REVEAL_MS * 2)
      return
    }
    // 停留时长按字数走：长句子需要更久才读得完
    const dwell = Math.min(12_000, 2600 + this.fullText().length * 90)
    this.hideTimer = window.setTimeout(() => this.clear(), dwell)
  }

  private stop(): void {
    clearTimeout(this.timer)
    this.timer = 0
  }

  private flushToLines(): void {
    const cur = (this.shown + this.pending).trim()
    if (cur) this.lines.push(cur)
    this.shown = ''
    this.pending = ''
    this.el.classList.remove('error')
  }

  private fullText(): string {
    return [...this.lines, this.shown].filter(Boolean).join('\n')
  }

  private render(): void {
    this.textEl.textContent = this.fullText()
  }
}
