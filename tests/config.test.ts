import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'

import {
  DEFAULT_CONFIG, readConfigDirectory, writeConfigDirectory,
} from '../src/main/config.ts'

const temporaryDirectories: string[] = []

function makeTemporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'yueli-config-'))
  temporaryDirectories.push(directory)
  return directory
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true })
  }
})

describe('拆分配置', () => {
  it('按厂商、模型、Bot 和功能四个职责写入并可无损读回', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = structuredClone(DEFAULT_CONFIG)
    config.bot.name = '测试月璃'
    config.llm.provider = 'deepseek'
    config.llm.model = 'deepseek-v4-flash'
    config.llm.api_key = 'sk-test'
    config.vision.enabled = true
    config.personality.identity = '自定义身份'
    config.conversation.working_memory_messages = 60
    config.generation.chat.temperature = 0.42
    config.llm.max_retries = 4
    config.vision.max_retries = 4

    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory)).toEqual(config)
    expect(readFileSync(join(directory, 'providers.toml'), 'utf-8')).toContain('[[api_providers]]')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('model_identifier = "deepseek-v4-flash"')
    expect(readFileSync(join(directory, 'bot.toml'), 'utf-8')).toContain("identity = '''自定义身份'''")
    expect(readFileSync(join(directory, 'bot.toml'), 'utf-8')).toContain('working_memory_messages = 60')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('temperature = 0.42')
    expect(readFileSync(join(directory, 'providers.toml'), 'utf-8')).toContain('max_retries = 4')
    expect(readFileSync(join(directory, 'features.toml'), 'utf-8')).toContain('enabled = true')
    expect(readFileSync(join(directory, 'features.toml'), 'utf-8')).toContain('frames = 1')
  })

  it('自动迁移旧 config.toml 并保留旧文件', () => {
    const root = makeTemporaryDirectory()
    const legacyPath = join(root, 'config.toml')
    const directory = join(root, 'config')
    writeFileSync(legacyPath, `
[bot]
user_nickname = "小明"
relationship = "哥哥"

[llm]
provider = "deepseek"
model = "deepseek-chat"
api_key = "sk-legacy"
`, 'utf-8')

    const config = readConfigDirectory(directory, legacyPath)

    expect(config.bot.name).toBe('月璃')
    expect(config.bot.user_nickname).toBe('小明')
    expect(config.llm.model).toBe('deepseek-chat')
    expect(readFileSync(legacyPath, 'utf-8')).toContain('sk-legacy')
    expect(readFileSync(join(directory, 'providers.toml'), 'utf-8')).toContain('sk-legacy')
  })

  it('配置目录不完整时明确报错，不静默回退默认值', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    mkdirSync(directory)
    writeFileSync(join(directory, 'providers.toml'), '', 'utf-8')

    expect(() => readConfigDirectory(directory)).toThrow('缺少配置文件')
  })
})
