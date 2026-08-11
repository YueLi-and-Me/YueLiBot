/**
 * 读取、更新和写入角色立绘素材清单。
 *
 * 清单类型来自 electron/shared/sprite-manifest.ts，生成流程通过本模块登记底图、
 * 表情和动作文件，渲染层随后使用同一份 JSON 进行资源定位。
 */
import { readFile, writeFile } from 'node:fs/promises'
import type { CharPaths } from './paths.ts'

// 类型定义复用 electron/shared，生成脚本与渲染层据此共享同一份清单结构。
export type { Rect, SpriteManifest } from '../../electron/shared/sprite-manifest.ts'
import type { SpriteManifest } from '../../electron/shared/sprite-manifest.ts'

/**
 * 将角色素材清单序列化为格式化 JSON。
 *
 * @param paths 角色素材目录路径集合。
 * @param m 已完成资源登记的清单对象。
 * @returns 文件写入完成后的 Promise。
 * @throws Error 目标目录不存在或文件不可写。
 * @sideEffects 覆盖 ``paths.manifest``，写入末尾换行。
 */
export async function writeManifest(paths: CharPaths, m: SpriteManifest): Promise<void> {
  await writeFile(paths.manifest, `${JSON.stringify(m, null, 2)}\n`, 'utf8')
}

/**
 * 读取角色素材清单并解析 JSON。
 *
 * @param paths 角色素材目录路径集合。
 * @returns 已解析清单；文件缺失、读取失败或 JSON 无法解析时返回 ``null``。
 */
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

/**
 * 读取任意 JSON 状态文件，并在不可读时返回调用方提供的默认值。
 *
 * @param file JSON 文件路径。
 * @param fallback 文件缺失、读取失败或解析失败时的返回值。
 * @returns 解析后的泛型值或 ``fallback``。
 */
export async function readJson<T>(file: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await readFile(file, 'utf8')) as T
  } catch {
    return fallback
  }
}

/**
 * 将状态对象写入格式化 JSON 文件。
 *
 * @param file 目标文件路径。
 * @param data 可 JSON 序列化的数据。
 * @returns 文件写入完成后的 Promise。
 * @throws Error 目录不存在、数据不可序列化或文件不可写。
 */
export async function writeJson(file: string, data: unknown): Promise<void> {
  await writeFile(file, `${JSON.stringify(data, null, 2)}\n`, 'utf8')
}
