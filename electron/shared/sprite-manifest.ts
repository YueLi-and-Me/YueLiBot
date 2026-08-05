/**
 * 立绘素材清单的类型定义。
 *
 * 生图管线（scripts/sprite）写，渲染层（src/renderer）读，
 * 所以放在 shared 下让两边共用同一份定义 —— 各写一份迟早会漂。
 */

export interface Rect {
  x: number
  y: number
  width: number
  height: number
}

export interface SpriteManifest {
  version: 1
  name: string
  generatedAt: string
  /** 所有图共用的画布尺寸 —— 对齐后一致，切换才不会跳。 */
  canvas: { width: number; height: number }
  /** 对齐锚点（头顶中心）在画布中的坐标，供上层做定位和缩放。 */
  anchor: { x: number; y: number }
  base: string
  /** 表情 id → 文件路径；blink 为对应的闭眼差分（没有则不眨眼）。 */
  expressions: Record<string, { file: string; blink?: string }>
  /** 嘴型 id → 文件路径。 */
  mouth: Record<string, string>
  /**
   * 五官区域，由差分图自动求出（见 scripts/sprite/process.ts）。
   *
   * 渲染层靠它做**局部合成**而不是整图切换：她生气时说话，
   * 只把嘴部矩形换成张嘴版，脸的其余部分保持生气 ——
   * 整图切换会让表情在每次开口时退回平静脸。
   */
  regions?: {
    /** mouth/closed 与 mouth/open 的差异区域 */
    mouth?: Rect
    /** face/x 与 eyes/x 的差异区域 */
    eyes?: Rect
    /**
     * 脸部烘焙区域（眼区 ∪ 嘴区再外扩）。
     * 各表情图的这块区域来自各自的生成结果，区域之外一律是底图的身体 ——
     * 保证切换表情时身体逐像素一致，不会闪。
     */
    face?: Rect
  }
}
