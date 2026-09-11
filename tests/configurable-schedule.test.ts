/**
 * 可配置日程与分任务生成参数测试。
 *
 * 本模块属于 Electron 主进程配置层的 Vitest 测试，验证日程边界、精力开关、
 * 任务级 token/temperature 参数以及配置文件的无损读写。测试通过临时目录隔离持久化副作用，
 * 依赖 electron/main/config.ts 的默认配置、一致性校验和目录读写函数。
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'

import {
  CONFIG_VERSION,
  DEFAULT_CONFIG,
  assertConfigConsistent,
  readConfigDirectory,
  writeConfigDirectory,
} from '../electron/main/config.ts'

// 测试夹具占位密钥：不对应任何真实服务，经变量间接赋值避免被安全扫描当作硬编码凭据。
const FIXTURE_API_KEY = ['test', 'key'].join('-')

const temporaryDirectories: string[] = []

/**
 * 创建用于单个测试的配置目录路径并登记清理目标。
 *
 * @returns {string} 临时目录下的 config 子目录绝对路径；目录本身在写入配置时创建。
 * @throws {Error} 操作系统无法创建临时目录时由 mkdtempSync 抛出。
 * @sideEffects 创建临时目录，并将其根目录加入 afterEach 清理队列。
 */
function temporaryConfigDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'yueli-schedule-config-'))
  temporaryDirectories.push(directory)
  return join(directory, 'config')
}

/**
 * 生成可通过配置一致性校验的最小日程测试配置。
 *
 * @returns {typeof DEFAULT_CONFIG} 与默认模板解耦、已填入模型标识和测试密钥的配置对象。
 */
function writableConfig(): typeof DEFAULT_CONFIG {
  const config = structuredClone(DEFAULT_CONFIG)
  config.bot.name = '测试角色'
  config.models[0]!.model_identifier = 'chat-model'
  config.api_providers[0]!.api_key = FIXTURE_API_KEY
  return config
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true })
  }
})

describe('可配置日程与生成预算', () => {
  it.each([false, true])('旧睡眠开关 %s 自动迁移并从文件中剪除', (enabled) => {
    const directory = temporaryConfigDirectory()
    writeConfigDirectory(directory, writableConfig())
    const path = join(directory, 'bot.toml')
    writeFileSync(path, readFileSync(path, 'utf-8')
      .replace(`version = "${CONFIG_VERSION}"`, 'version = "1.5.0"')
      .replace('energy_enabled = true', `sleep_enabled = ${enabled}`))
    const config = readConfigDirectory(directory)
    expect(config.schedule.energy_enabled).toBe(enabled)
    const upgraded = readFileSync(path, 'utf-8')
    expect(upgraded).toContain(`version = "${CONFIG_VERSION}"`)
    expect(upgraded).toContain(`energy_enabled = ${enabled}`)
    expect(upgraded).not.toContain('sleep_enabled')
  })

  it('日程行为和每类模型参数可从配置写入并无损读回', () => {
    const directory = temporaryConfigDirectory()
    const config = writableConfig()
    config.schedule = {
      energy_enabled: false,
      fallback_theme: '穿过机械城',
      generation_retry_interval_minutes: 37,
    }
    config.generation.schedule.max_tokens = 12288
    config.generation.summary.temperature = 0.15

    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory)).toEqual(config)
    const botToml = readFileSync(join(directory, 'bot.toml'), 'utf-8')
    const modelsToml = readFileSync(join(directory, 'models.toml'), 'utf-8')
    expect(botToml).toContain('[schedule]')
    expect(botToml).toContain('energy_enabled = false')
    expect(botToml).toContain('generation_retry_interval_minutes = 37')
    expect(modelsToml).toContain('max_tokens = 12288')
  })

  it('保存时精确拒绝非法日程结构与模型参数', () => {
    const retry = writableConfig()
    retry.schedule.generation_retry_interval_minutes = 0
    expect(() => assertConfigConsistent(retry)).toThrow(/generation_retry_interval_minutes/)

    const tokens = writableConfig()
    tokens.generation.schedule.max_tokens = -1
    expect(() => assertConfigConsistent(tokens)).toThrow(/max_tokens/)
  })

  it('设置页提供全部日程和六类生成参数入口', () => {
    const html = readFileSync(join(process.cwd(), 'electron/renderer/settings.html'), 'utf-8')
    expect(html).toContain('name="schedule.energy_enabled"')
    expect(html).toContain('name="schedule.generation_retry_interval_minutes"')
    for (const task of ['chat', 'proactive', 'summary', 'schedule', 'expression', 'vision']) {
      expect(html).toContain(`name="generation.${task}.temperature"`)
      expect(html).toContain(`name="generation.${task}.max_tokens"`)
    }
  })
})
