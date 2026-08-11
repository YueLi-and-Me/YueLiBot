/**
 * 角色窗口指针交互模块。
 *
 * 本模块在渲染进程中把 Canvas 透明像素命中检测、带 `data-hit` 标记的 UI
 * 元素命中检测、窗口穿透切换、指针捕获、拖动手势和点击回调组合为一套状态机。
 * 窗口默认允许鼠标穿透；只有光标位于角色不透明像素或交互 UI 上时才切换为
 * 可交互状态，角色本体上的左键按下再根据位移阈值区分点击与拖动。
 *
 * 模块依赖浏览器的 Canvas 2D 像素读取 API、DOM `elementFromPoint` 与指针事件，
 * 并通过预加载层注入的 `window.pet.setInteractive`、`window.pet.beginDrag` 和
 * `window.pet.endDrag` 通知主进程。模块不负责创建 Canvas，也不负责移除监听器；
 * 调用方应在页面生命周期内只初始化一次。
 *
 * @module renderer/ui/pointer
 */

/** 超过这个位移才算拖动，否则算点击。太小会让手抖变成拖窗。 */
const DRAG_THRESHOLD = 4
/** 抠图边缘的低 alpha 像素不视为可交互区域。 */
const ALPHA_THRESHOLD = 24

/**
 * 指针交互初始化参数。
 *
 * @property {HTMLCanvasElement} canvas 提供角色图像和像素命中检测的 Canvas。
 * @property {() => void} onTap 无位移左键点击角色本体时执行的业务回调。
 */
export interface PointerOptions {
  canvas: HTMLCanvasElement
  /** 用户在角色本体上完成无位移左键点击时触发。 */
  onTap: () => void
}

/**
 * 初始化角色像素命中、窗口穿透切换和拖动手势监听器。
 *
 * @param {PointerOptions} options 初始化参数对象。
 * @param {HTMLCanvasElement} options.canvas 用于角色像素命中检测的 Canvas；其
 * `width` 与 `height` 由 Canvas 自身维护；任一尺寸为 `0` 时角色像素不会命中，
 * CSS 缩放由命中检测逻辑自动换算。
 * @param {() => void} options.onTap 角色本体完成无位移左键点击时执行的回调；
 * 回调不接收坐标参数，默认由调用方提供业务动作。
 * @returns {void} 无返回值；事件监听器在当前页面生命周期内持续工作。
 * @throws {TypeError} 运行环境缺少可用的 Canvas 2D 上下文时，后续角色像素
 * 命中检测访问空上下文会抛出类型错误；初始化阶段不会主动校验上下文。
 * @throws {Error} 注册的事件回调执行期间，预加载层窗口桥接方法或 `onTap` 回调
 * 主动抛错时，异常不会被本模块捕获。
 * @remarks 方法会注册 `window` 与 `canvas` 的指针监听器，切换主进程窗口穿透
 * 状态，维护指针捕获，并在拖动开始和结束时分别调用主进程桥接方法。监听器没有
 * 对应的销毁函数，重复初始化会重复处理事件并增加像素读取开销。
 */
export function setupPointer({ canvas, onTap }: PointerOptions): void {
  const ctx = canvas.getContext('2d', { willReadFrequently: true })!
  let interactive = false
  let dragging = false
  let armed = false
  let startX = 0
  let startY = 0

  /**
   * 在窗口穿透状态发生变化时同步渲染进程和主进程的交互状态。
   *
   * @param {boolean} on `true` 表示窗口接收指针事件，`false` 表示恢复窗口
   * 穿透；状态值只允许使用布尔值。
   * @returns {void} 无返回值；状态未变化时直接返回。
   * @throws {Error} 预加载层 `setInteractive` 桥接方法抛错时，异常向事件处理
   * 器传播；本函数不提供兜底处理。
   * @remarks 状态去重可以避免每次 `pointermove` 都跨进程发送相同设置，同时
   * 更新光标样式，使拖动开始前后的用户反馈与窗口状态保持一致。
   */
  const setInteractive = (on: boolean): void => {
    if (on === interactive) return
    interactive = on
    window.pet?.setInteractive(on)
    document.body.style.cursor = on ? 'grab' : 'default'
  }

  /**
   * 判断视口坐标是否落在角色的不透明像素上。
   *
   * @param {number} clientX 相对于浏览器视口左边缘的横坐标，单位为 CSS 像素；
   * 可为负数或超出视口范围，越界结果会判定为未命中。
   * @param {number} clientY 相对于浏览器视口上边缘的纵坐标，单位为 CSS 像素；
   * 可为负数或超出视口范围，越界结果会判定为未命中。
   * @returns {boolean} 命中 Canvas 内 alpha 值大于 `ALPHA_THRESHOLD` 的像素时
   * 返回 `true`，Canvas 尺寸无效、坐标越界或像素透明度不足时返回 `false`。
   * @throws {TypeError|DOMException} Canvas 上下文为空或像素读取失败时，由
   * `getImageData` 传播浏览器异常；例如上下文不可用或读取受安全策略限制。
   * @remarks 每次调用只读取一个像素，但 `pointermove` 可能高频触发；Canvas
   * 上下文通过 `willReadFrequently` 创建，以降低连续像素读取的实现开销。
   */
  const onCharacter = (clientX: number, clientY: number): boolean => {
    const rect = canvas.getBoundingClientRect()
    if (!rect.width || !rect.height) return false
    // 先换算到 Canvas 的内部像素坐标，避免 CSS 缩放导致命中点与实际图像错位。
    const x = Math.round(((clientX - rect.left) / rect.width) * canvas.width)
    const y = Math.round(((clientY - rect.top) / rect.height) * canvas.height)
    if (x < 0 || y < 0 || x >= canvas.width || y >= canvas.height) return false
    return ctx.getImageData(x, y, 1, 1).data[3]! > ALPHA_THRESHOLD
  }

  /**
   * 判断视口坐标是否位于需要交互的 UI 元素上。
   *
   * 窗口默认整体穿透，而输入栏可能位于透明区域；仅检查角色 alpha 会使输入栏
   * 无法接收点击。因此优先使用 elementFromPoint 查找带 data-hit 的 UI 元素，
   * 同时尊重层叠顺序和 pointer-events。
   *
   * @param {number} clientX 相对于浏览器视口左边缘的横坐标，单位为 CSS 像素。
   * @param {number} clientY 相对于浏览器视口上边缘的纵坐标，单位为 CSS 像素。
   * @returns {boolean} 最上层命中元素或其祖先包含 `[data-hit]` 属性时返回
   * `true`，否则返回 `false`。
   * @remarks 该方法只依赖 DOM 命中结果，不读取 Canvas 像素；UI 命中优先于角色
   * 命中，使透明区域中的输入栏等控件仍可接收指针事件。
   */
  const onInteractiveUi = (clientX: number, clientY: number): boolean =>
    !!document.elementFromPoint(clientX, clientY)?.closest('[data-hit]')

  /**
   * 合并 UI 元素命中和角色像素命中结果。
   *
   * @param {number} clientX 相对于浏览器视口左边缘的横坐标，单位为 CSS 像素。
   * @param {number} clientY 相对于浏览器视口上边缘的纵坐标，单位为 CSS 像素。
   * @returns {boolean} 坐标命中交互 UI 或角色不透明像素时返回 `true`。
   * @throws {TypeError|DOMException} 角色像素读取失败时，异常由
   * {@link onCharacter} 传播。
   * @remarks 采用短路求值，命中 UI 时不会再次读取 Canvas 像素，以减少高频指针
   * 移动下的同步像素读取。
   */
  const hitTest = (clientX: number, clientY: number): boolean =>
    onInteractiveUi(clientX, clientY) || onCharacter(clientX, clientY)

  window.addEventListener('pointermove', (e) => {
    // 拖动或待判定手势期间保持交互状态，避免恢复穿透后丢失 pointerup。
    if (dragging || armed) return
    setInteractive(hitTest(e.clientX, e.clientY))
  })

  window.addEventListener('pointerleave', () => {
    if (dragging || armed) return
    setInteractive(false)
  })

  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return
    // 拖动只从角色本体开始；输入栏的按下事件必须保留文本选择与编辑语义。
    if (!onCharacter(e.clientX, e.clientY)) return
    armed = true
    startX = e.clientX
    startY = e.clientY
    // 捕获指针使光标离开窗口后仍能收到 move/up，确保拖动状态可以闭合。
    canvas.setPointerCapture(e.pointerId)
  })

  canvas.addEventListener('pointermove', (e) => {
    if (!armed) return
    if (!dragging) {
      if (Math.abs(e.clientX - startX) < DRAG_THRESHOLD && Math.abs(e.clientY - startY) < DRAG_THRESHOLD) return
      dragging = true
      document.body.style.cursor = 'grabbing'
      // 主进程读取屏幕绝对坐标；不传浏览器增量，避免窗口移动引入坐标系反馈。
      window.pet?.beginDrag()
    }
  })

  /**
   * 收束一次已捕获的指针手势，并区分拖动结束与点击完成。
   *
   * @param {PointerEvent} e 触发收束的 `pointerup` 或 `pointercancel` 事件；其
   * `pointerId` 必须对应当前 Canvas 捕获的指针。
   * @returns {void} 无返回值；未进入待判定状态时直接返回。
   * @throws {Error} 预加载层 `endDrag` 桥接方法或 `onTap` 回调抛错时，异常向
   * 事件处理器传播；本函数不捕获业务异常。
   * @remarks 方法会释放指针捕获并重置光标；拖动手势只通知主进程结束，未发生
   * 位移的手势才调用点击回调，从而避免一次拖动同时触发点击业务。
   */
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
