/**
 * 角色渲染抽象层。
 *
 * 上层逻辑（记忆、人格、日程、打扰调度）只认这个接口，
 * 完全不知道底下是 Live2D、AI 立绘差分还是别的什么。
 * 换渲染方案时改的只有实现类，其余代码一行不动。
 */
import type { Emotion, Gesture, OutfitItem } from '../../shared/character-vocab.ts'

// 词表在 shared 下，core 与 renderer 共用同一份；这里原样转出，
// 渲染层代码继续从本模块导入即可
export * from '../../shared/character-vocab.ts'

export interface CharacterView {
  /** face 通道。互斥切换，实现内部做淡入淡出，不要硬切。 */
  setEmotion(emotion: Emotion): void

  /**
   * gesture 通道。播放一个瞬时动作，durationMs 后自动回落。
   * 不传时长则用实现的默认值。
   */
  playGesture(gesture: Gesture, durationMs?: number): void

  /** 口型开合 0~1，由 TTS 音频的 RMS 驱动。无 TTS 时保持 0。 */
  setMouthOpen(value: number): void

  /**
   * 视线跟随。坐标是相对画布中心的归一化值，范围 -1~1。
   * AI 立绘差分没有眼球参数，该实现下降级为空操作 —— 这是接口契约的一部分，
   * 调用方不需要判断当前是哪种实现。
   */
  lookAt(x: number, y: number): void

  /** outfit 通道。传入完整集合（而非增量），实现内部做差异更新。 */
  setOutfit(items: readonly OutfitItem[]): void

  /** 当前是否可用。资源没加载完或加载失败时为 false。 */
  readonly ready: boolean

  /** 释放 GPU 资源与定时器。 */
  destroy(): void
}

/** 各实现共用的构造选项。 */
export interface CharacterViewOptions {
  canvas: HTMLCanvasElement
  /** 素材根目录的 URL 前缀。 */
  assetsBase: string
  /** 表情切换的淡入淡出时长。硬切会很跳。 */
  transitionMs?: number
  /** 自动眨眼的平均间隔；设为 0 关闭。 */
  blinkIntervalMs?: number
}
