import { mkdir } from 'node:fs/promises'
import { resolve } from 'node:path'

/**
 * 素材目录约定。
 *
 *   .sprite-work/<name>/     生图中间产物（白底、未对齐）—— 已 gitignore
 *     base-1..N.png          底图候选
 *     base.png               定稿底图
 *     face/ eyes/ mouth/
 *     state.json             已完成条目，支撑断点续跑
 *     review.json            预览页标记的重跑清单
 *
 *   assets/character/<name>/ 最终产物（透明背景、已对齐）
 *     base.png
 *     face/ eyes/ mouth/
 *     manifest.json
 *
 * 中间产物刻意放在 assets/ **之外**：assets/ 被 Vite 当静态资源根目录，
 * 放里面会把几十兆的白底原图一并打进构建产物。
 */
export class CharPaths {
  readonly root: string
  readonly raw: string

  constructor(readonly name: string) {
    this.root = resolve('assets', 'character', name)
    this.raw = resolve('.sprite-work', name)
  }

  rawBaseCandidate(i: number) {
    return resolve(this.raw, `base-${i}.png`)
  }
  get rawBase() {
    return resolve(this.raw, 'base.png')
  }
  rawFile(kind: Kind, id: string) {
    return resolve(this.raw, kind, `${id}.png`)
  }
  get state() {
    return resolve(this.raw, 'state.json')
  }
  get review() {
    return resolve(this.raw, 'review.json')
  }

  get base() {
    return resolve(this.root, 'base.png')
  }
  outFile(kind: Kind, id: string) {
    return resolve(this.root, kind, `${id}.png`)
  }
  get manifest() {
    return resolve(this.root, 'manifest.json')
  }

  async ensureRawDirs() {
    for (const d of [this.raw, ...KINDS.map((k) => resolve(this.raw, k))]) {
      await mkdir(d, { recursive: true })
    }
  }
  async ensureOutDirs() {
    for (const d of [this.root, ...KINDS.map((k) => resolve(this.root, k))]) {
      await mkdir(d, { recursive: true })
    }
  }
}

export const KINDS = ['face', 'eyes', 'mouth'] as const
export type Kind = (typeof KINDS)[number]

/** 素材条目的稳定标识，形如 "face/happy"。state/review 都按它索引。 */
export function slug(kind: Kind, id: string): string {
  return `${kind}/${id}`
}
