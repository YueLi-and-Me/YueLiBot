import { readFile, writeFile } from 'node:fs/promises'
import type { CharPaths } from './paths.ts'

// 类型定义放在 electron/shared，与渲染层共用同一份 —— 各写一份迟早会漂
export type { Rect, SpriteManifest } from '../../electron/shared/sprite-manifest.ts'
import type { SpriteManifest } from '../../electron/shared/sprite-manifest.ts'

export async function writeManifest(paths: CharPaths, m: SpriteManifest): Promise<void> {
  await writeFile(paths.manifest, `${JSON.stringify(m, null, 2)}\n`, 'utf8')
}

export async function readManifest(paths: CharPaths): Promise<SpriteManifest | null> {
  try {
    return JSON.parse(await readFile(paths.manifest, 'utf8')) as SpriteManifest
  } catch {
    return null
  }
}

// --- 生成期的两份状态文件（都只在 raw/ 下，不进版本库）-------------------

/** 已成功生成的条目，支撑断点续跑：中断后重跑只补缺的，不重复计费。 */
export interface GenState {
  /** slug → 完成信息 */
  done: Record<string, { at: string; bytes: number; attempts: number }>
}

/** 预览页标记的重跑清单：slug → 问题描述（会被回喂给模型做强化指令）。 */
export type ReviewList = Record<string, string>

export async function readJson<T>(file: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await readFile(file, 'utf8')) as T
  } catch {
    return fallback
  }
}

export async function writeJson(file: string, data: unknown): Promise<void> {
  await writeFile(file, `${JSON.stringify(data, null, 2)}\n`, 'utf8')
}
