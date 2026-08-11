/**
 * 根据角色 Canvas 的实际渲染框同步输入栏和消息气泡的几何位置。
 *
 * 本模块属于渲染进程 UI 布局层，使用 Canvas 的实际边界计算浮层尺寸，
 * 并通过 ResizeObserver 与 window.resize 同步窗口变化和角色素材尺寸变化。
 * 它不依赖业务状态；调用方负责传入已挂载的 Canvas、输入栏和可选气泡元素。
 */

/** 输入栏相对角色宽度的占比。 */
const WIDTH_RATIO = 0.94
/** 输入栏宽度上下限，避免极端窗口尺寸导致不可用宽度或遮挡角色。 */
const MIN_WIDTH = 150
const MAX_WIDTH = 260
/** 输入栏距角色顶部的比例，让它稳定落在头顶附近而不是脚边。 */
const HEAD_OFFSET_RATIO = 0.035
const MIN_HEAD_OFFSET = 10
const MAX_HEAD_OFFSET = 26

/**
 * 监听角色画布尺寸并同步浮层位置。
 *
 * @param canvas 角色实际渲染的 Canvas 元素。
 * @param composer 输入栏元素。
 * @param bubble 可选消息气泡元素；省略时只同步输入栏。
 * @returns 取消 ResizeObserver 和 window.resize 监听的清理函数。
 * @sideEffects 修改浮层的 width、left、top、right 和 bottom 样式，并注册尺寸监听器。
 */
export function syncComposerToCharacter(
  canvas: HTMLCanvasElement,
  composer: HTMLElement,
  bubble?: HTMLElement,
): () => void {
  /**
   * 根据当前 Canvas 边界重新计算输入栏和气泡的浮层位置。
   *
   * @returns {void} 无返回值；Canvas 宽度为零时跳过本轮布局。
   * @remarks 位置计算使用 Canvas 的实际 CSS 边界而不是固定坐标，以适应窗口缩放
   *   和角色素材尺寸变化；每次调用都会同步写入浮层样式。
   */
  const apply = (): void => {
    const box = canvas.getBoundingClientRect()
    if (!box.width) return

    const width = Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, box.width * WIDTH_RATIO)))
    const headTop = Math.round(
      box.top + Math.min(MAX_HEAD_OFFSET, Math.max(MIN_HEAD_OFFSET, box.height * HEAD_OFFSET_RATIO)),
    )
    // 与角色同中线，并把输入栏放到角色头顶附近。
    const left = Math.round(box.left + (box.width - width) / 2)

    composer.style.width = `${width}px`
    composer.style.left = `${left}px`
    composer.style.top = `${headTop}px`
    // 覆盖掉 CSS 里的 right/bottom，否则 left+right+width 或 top+bottom+height 会冲突
    composer.style.right = 'auto'
    composer.style.bottom = 'auto'

    if (bubble) {
      // 气泡放在角色头顶左侧，右边缘与输入栏留出间距，两个浮层同时出现时也不会遮挡。
      const bubbleWidth = bubble.getBoundingClientRect().width
      const bubbleLeft = Math.max(6, Math.round(box.left - bubbleWidth - 12))
      bubble.style.left = `${bubbleLeft}px`
      bubble.style.top = `${headTop}px`
      bubble.style.right = 'auto'
      bubble.style.bottom = 'auto'
    }
  }

  apply()
  // 画布尺寸随窗口变，用 ResizeObserver 而不是 window.resize：
  // 前者在 canvas 自身尺寸变化（比如换了套素材）时也会触发
  const ro = new ResizeObserver(apply)
  ro.observe(canvas)
  addEventListener('resize', apply)

  return () => {
    ro.disconnect()
    removeEventListener('resize', apply)
  }
}
