/**
 * 指针交互：点击穿透的命中检测 + 拖动窗口 + 区分「点」和「拖」。
 *
 * 三件事必须放在一起处理，因为它们互相干扰：
 *  · 命中检测靠光标下的像素 alpha 决定窗口吃不吃鼠标事件
 *  · 拖动过程中光标经常会跑到角色轮廓之外，此时若照常做命中检测，
 *    窗口会立刻恢复穿透 → 收不到 pointerup → 拖动卡住不放手
 *  · 桌宠身上既要能拖又要能点，得靠位移阈值区分
 */

/** 超过这个位移才算拖动，否则算点击。太小会让手抖变成拖窗。 */
const DRAG_THRESHOLD = 4
/** 抠图边缘那圈半透明杂点不该算命中。 */
const ALPHA_THRESHOLD = 24

export interface PointerOptions {
  canvas: HTMLCanvasElement
  /** 在角色身上「点了一下」（没有拖动）时触发。 */
  onTap: () => void
}

export function setupPointer({ canvas, onTap }: PointerOptions): void {
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!
  let interactive = false
  let dragging = false
  let armed = false
  let startX = 0
  let startY = 0

  const setInteractive = (on: boolean): void => {
    if (on === interactive) return
    interactive = on
    window.pet?.setInteractive(on)
    document.body.style.cursor = on ? 'grab' : 'default'
  }

  /** 光标是否压在角色的不透明像素上。 */
  const onCharacter = (clientX: number, clientY: number): boolean => {
    const rect = canvas.getBoundingClientRect()
    if (!rect.width || !rect.height) return false
    // 屏幕坐标 → 画布内部坐标（canvas 被 CSS 缩放过，比例不是 1:1）
    const x = Math.round(((clientX - rect.left) / rect.width) * canvas.width)
    const y = Math.round(((clientY - rect.top) / rect.height) * canvas.height)
    if (x < 0 || y < 0 || x >= canvas.width || y >= canvas.height) return false
    return ctx.getImageData(x, y, 1, 1).data[3]! > ALPHA_THRESHOLD
  }

  /**
   * 光标是否压在需要交互的 UI 上（输入栏等）。
   *
   * ★ 这一条不能漏。窗口默认整体穿透，只有命中检测说「在角色身上」才恢复接收
   * 鼠标事件。而输入栏在角色头顶附近，仍可能落在**透明区域、不是角色像素** ——
   * 只按角色 alpha 判断的话，光标一移到输入栏窗口立刻恢复穿透，
   * 点击直接穿到桌面上，输入框**根本点不到、也就永远打不进字**。
   *
   * 用 elementFromPoint 而不是逐个元素比矩形：元素多了以后前者不会漏，
   * 也自动尊重层叠与 pointer-events。
   */
  const onInteractiveUi = (clientX: number, clientY: number): boolean =>
    !!document.elementFromPoint(clientX, clientY)?.closest('[data-hit]')

  const hitTest = (clientX: number, clientY: number): boolean =>
    onInteractiveUi(clientX, clientY) || onCharacter(clientX, clientY)

  window.addEventListener('pointermove', (e) => {
    if (dragging || armed) return // 拖动中不动交互状态，否则会中途失去 pointerup
    setInteractive(hitTest(e.clientX, e.clientY))
  })

  window.addEventListener('pointerleave', () => {
    if (dragging || armed) return
    setInteractive(false)
  })

  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return
    // 拖动只认角色本体 —— 在输入栏上按下应该是选文字，不是拖窗
    if (!onCharacter(e.clientX, e.clientY)) return
    armed = true
    startX = e.clientX
    startY = e.clientY
    // 捕获指针：拖到窗口外也照样能收到 move/up，否则松手时窗口会「粘」在光标上
    canvas.setPointerCapture(e.pointerId)
  })

  canvas.addEventListener('pointermove', (e) => {
    if (!armed) return
    if (!dragging) {
      if (Math.abs(e.clientX - startX) < DRAG_THRESHOLD && Math.abs(e.clientY - startY) < DRAG_THRESHOLD) return
      dragging = true
      document.body.style.cursor = 'grabbing'
      // 主进程读屏幕光标绝对坐标来移动窗口。
      // 不传增量：窗口一动浏览器坐标系跟着动，增量会自我干扰、拖起来发抖
      window.pet?.beginDrag()
    }
  })

  const finish = (e: PointerEvent): void => {
    if (!armed) return
    armed = false
    if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId)

    if (dragging) {
      dragging = false
      window.pet?.endDrag()
      document.body.style.cursor = 'grab'
      return
    }
    onTap()
  }

  canvas.addEventListener('pointerup', finish)
  canvas.addEventListener('pointercancel', finish)
}
