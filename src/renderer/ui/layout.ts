/**
 * 让输入栏跟着角色走。
 *
 * 角色是 `object-fit: contain` 渲染的，实际宽度由**窗口高度**决定 ——
 * 写死 `right: 6px; width: 190px` 只在某一个特定窗口尺寸下恰好对齐，
 * 改一下 PET_W/PET_H 或者换个立绘比例就偏了。
 *
 * 所以按角色的实际渲染框实时算。
 */

/** 输入栏相对角色宽度的占比。窄于她一点，视觉上像「在她脚边」而不是把她框住。 */
const WIDTH_RATIO = 0.94
/** 上下限，避免窗口极端尺寸下变成一条细缝或者撑爆。 */
const MIN_WIDTH = 150
const MAX_WIDTH = 260

export function syncComposerToCharacter(canvas: HTMLCanvasElement, composer: HTMLElement): () => void {
  const apply = (): void => {
    const box = canvas.getBoundingClientRect()
    if (!box.width) return

    const width = Math.round(Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, box.width * WIDTH_RATIO)))
    // 与角色同中线
    const left = Math.round(box.left + (box.width - width) / 2)

    composer.style.width = `${width}px`
    composer.style.left = `${left}px`
    // 覆盖掉 CSS 里的 right，否则 left+right+width 三者冲突
    composer.style.right = 'auto'
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
