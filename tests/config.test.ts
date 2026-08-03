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

describe('豆包语音 TTS', () => {
  it('保存后不再把 client_type 和 app_id 抹回 openai', () => {
    // 回归：serializeProviders 里 tts 那条曾经硬编码 client_type: 'openai' 且
    // 不写 app_id。手改成豆包语音后，只要在设置窗口保存一次（哪怕只改昵称），
    // 这两项就会被静默抹掉，TTS 悄悄退回 OpenAI 协议然后失败。
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = structuredClone(DEFAULT_CONFIG)
    config.tts.enabled = true
    config.tts.client_type = 'volcengine'
    config.tts.app_id = 'my-app-id'
    config.tts.api_key = 'my-access-token'
    config.tts.voice = 'zh_female_test'
    config.tts.cluster = 'volcano_tts'

    writeConfigDirectory(directory, config)
    const readBack = readConfigDirectory(directory)

    expect(readBack.tts.client_type).toBe('volcengine')
    expect(readBack.tts.app_id).toBe('my-app-id')
    expect(readBack.tts.cluster).toBe('volcano_tts')
    expect(readBack).toEqual(config)

    // 再存一次也不能漂移——设置窗口每次保存都会整份重写
    writeConfigDirectory(directory, readBack)
    expect(readConfigDirectory(directory)).toEqual(config)
  })

  it('providers.toml 里出现 volcengine 时不再拒绝启动', () => {
    // 回归：parseProviders 曾经对 client_type !== 'openai' 直接 throw，
    // 手写豆包语音配置会让 Electron 启动就失败，而不只是保存时被覆盖。
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = structuredClone(DEFAULT_CONFIG)
    config.tts.enabled = true
    config.tts.client_type = 'volcengine'
    config.tts.app_id = 'appid'
    config.tts.voice = 'zh_female_test'
    writeConfigDirectory(directory, config)

    const providers = readFileSync(join(directory, 'providers.toml'), 'utf-8')
    expect(providers).toContain('client_type = "volcengine"')
    expect(providers).toContain('app_id = "appid"')
    expect(() => readConfigDirectory(directory)).not.toThrow()
  })

  it('默认仍是 openai 协议，且不写出无关的 app_id', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = structuredClone(DEFAULT_CONFIG)
    config.tts.enabled = true
    config.tts.base_url = 'https://api.example.com/v1'
    config.tts.model = 'tts-1'
    config.tts.voice = 'alloy'
    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory).tts.client_type).toBe('openai')
    // app_id 只对 volcengine 有意义，openai 连接不该冒出这一行
    const providers = readFileSync(join(directory, 'providers.toml'), 'utf-8')
    expect(providers).not.toContain('app_id')
  })
})
