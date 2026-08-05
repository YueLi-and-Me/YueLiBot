/**
 * 让输入栏和消息气泡跟着角色走。
 *
 * 角色是 `object-fit: contain` 渲染的，实际宽度由**窗口高度**决定 ——
 * 写死 `right: 6px; width: 190px` 只在某一个特定窗口尺寸下恰好对齐，
 * 改一下 PET_W/PET_H 或者换个立绘比例就偏了。
 *
 * 所以按角色的实际渲染框实时算。
 */

/** 输入栏相对角色宽度的占比。窄于她一点，视觉上像贴在头顶的陪伴提示。 */
const WIDTH_RATIO = 0.94
/** 上下限，避免窗口极端尺寸下变成一条细缝或者撑爆。 */
const MIN_WIDTH = 150
const MAX_WIDTH = 260
/** 输入栏距角色顶部的比例，让它稳定落在头顶附近而不是脚边。 */
const HEAD_OFFSET_RATIO = 0.035
const MIN_HEAD_OFFSET = 10
const MAX_HEAD_OFFSET = 26

export function syncComposerToCharacter(
  canvas: HTMLCanvasElement,
  composer: HTMLElement,
  bubble?: HTMLElement,
): () => void {
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
