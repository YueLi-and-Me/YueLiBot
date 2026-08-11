/**
 * 统一解析角色素材生成流程使用的工作目录、输入文件和输出清单路径。
 *
 * base、gen、process 和 preview 脚本共享这些路径规则，避免中间产物、透明图和
 * manifest 写入不同目录；目录创建由调用方在执行写入前完成。
 */
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

  /**
   * 创建指定角色的工作路径集合。
   *
   * @param name 角色目录名；调用方应传入不包含路径分隔符的稳定名称。
   */
  constructor(readonly name: string) {
    this.root = resolve('assets', 'character', name)
    this.raw = resolve('.sprite-work', name)
  }

  /**
   * 返回指定编号的底图候选中间产物路径。
   *
   * @param i 候选编号，必须为正整数。
   * @returns {string} ``.sprite-work/<name>/base-<i>.png`` 的绝对路径。
   */
  rawBaseCandidate(i: number): string {
    return resolve(this.raw, `base-${i}.png`)
  }
  /**
   * 返回定稿底图的中间产物路径。
   *
   * @returns {string} 工作目录中的 ``base.png`` 绝对路径。
   */
  get rawBase(): string {
    return resolve(this.raw, 'base.png')
  }
  /**
   * 返回指定素材类别和标识的中间产物路径。
   *
   * @param kind 素材类别，仅允许 ``face``、``eyes`` 或 ``mouth``。
   * @param id 素材稳定标识，不应包含路径分隔符。
   * @returns {string} 对应中间 PNG 文件的绝对路径。
   */
  rawFile(kind: Kind, id: string): string {
    return resolve(this.raw, kind, `${id}.png`)
  }
  /**
   * 返回断点续跑状态文件路径。
   *
   * @returns {string} 工作目录中的 ``state.json`` 绝对路径。
   */
  get state(): string {
    return resolve(this.raw, 'state.json')
  }
  /**
   * 返回预览复核清单文件路径。
   *
   * @returns {string} 工作目录中的 ``review.json`` 绝对路径。
   */
  get review(): string {
    return resolve(this.raw, 'review.json')
  }

  /**
   * 返回最终资源目录中的底图路径。
   *
   * @returns {string} 最终资源目录中的 ``base.png`` 绝对路径。
   */
  get base(): string {
    return resolve(this.root, 'base.png')
  }
  /**
   * 返回最终资源目录中指定素材文件的路径。
   *
   * @param kind 素材类别，仅允许 ``face``、``eyes`` 或 ``mouth``。
   * @param id 素材稳定标识，不应包含路径分隔符。
   * @returns {string} 对应最终 PNG 文件的绝对路径。
   */
  outFile(kind: Kind, id: string): string {
    return resolve(this.root, kind, `${id}.png`)
  }
  /**
   * 返回最终素材清单 JSON 路径。
   *
   * @returns {string} 最终资源目录中的 ``manifest.json`` 绝对路径。
   */
  get manifest(): string {
    return resolve(this.root, 'manifest.json')
  }

  /**
   * 创建中间产物根目录及所有素材类别子目录。
   *
   * @returns {Promise<void>} 所有目录创建完成后的 Promise。
   * @throws Error 目录创建失败或目标路径不可写时抛出。
   * @sideEffects 创建 ``.sprite-work/<name>`` 及其素材类别子目录。
   */
  async ensureRawDirs(): Promise<void> {
    for (const d of [this.raw, ...KINDS.map((k) => resolve(this.raw, k))]) {
      await mkdir(d, { recursive: true })
    }
  }
  /**
   * 创建最终资源根目录及所有素材类别子目录。
   *
   * @returns {Promise<void>} 所有目录创建完成后的 Promise。
   * @throws Error 目录创建失败或目标路径不可写时抛出。
   * @sideEffects 创建 ``assets/character/<name>`` 及其素材类别子目录。
   */
  async ensureOutDirs(): Promise<void> {
    for (const d of [this.root, ...KINDS.map((k) => resolve(this.root, k))]) {
      await mkdir(d, { recursive: true })
    }
  }
}

export const KINDS = ['face', 'eyes', 'mouth'] as const
export type Kind = (typeof KINDS)[number]

/**
 * 生成素材条目的稳定索引键。
 *
 * @param kind 素材类别，仅允许 ``face``、``eyes`` 或 ``mouth``。
 * @param id 素材稳定标识，不应包含路径分隔符。
 * @returns {string} 形如 ``face/happy`` 的类别和标识组合键。
 */
export function slug(kind: Kind, id: string): string {
  return `${kind}/${id}`
}
