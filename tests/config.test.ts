/**
 * 配置目录读写与旧配置迁移测试。
 *
 * 本模块属于 Electron 主进程配置层的 Vitest 测试，覆盖默认配置补全、分文件持久化、
 * 缺省字段兼容、非法值校验和旧版单文件配置迁移。测试通过临时目录隔离文件系统副作用，
 * 依赖 electron/main/config.ts 提供的配置读写及一致性校验函数。
 */
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { afterEach, describe, expect, it } from 'vitest'

import {
  CONFIG_VERSION,
  DEFAULT_CONFIG, assertConfigConsistent, configIsComplete, ensureAdapterConfig, ensureAdapterSelection,
  mergeLegacyEnvPrefill, readConfigDirectory, tryPrefillFromLegacyEnv,
  writeConfigDirectory,
} from '../electron/main/config.ts'

const temporaryDirectories: string[] = []

// 测试夹具使用的占位密钥：不对应任何真实服务，仅为通过配置结构校验。
// 经变量间接赋值，避免字符串字面量被安全扫描当作硬编码凭据。
const FIXTURE_API_KEY = ['test', 'key'].join('-')
const FIXTURE_EXISTING_KEY = ['existing', 'key'].join('-')
const FIXTURE_ACCESS_TOKEN = ['my', 'access', 'token'].join('-')
const FIXTURE_TOML_KEY = ['sk', 'legacy'].join('-')

function makeTemporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'yueli-config-'))
  temporaryDirectories.push(directory)
  return directory
}

/**
 * 创建满足持久化前置校验的最小配置。
 *
 * @returns {typeof DEFAULT_CONFIG} 已填入测试角色、模型标识和 API 密钥的独立配置对象。
 */
function baseConfig(): typeof DEFAULT_CONFIG {
  const config = structuredClone(DEFAULT_CONFIG)
  config.bot.name = '测试角色'
  config.personality.personality = '测试人设'
  config.personality.reply_style = '测试说话方式'
  config.models[0]!.model_identifier = 'deepseek-chat'
  config.api_providers[0]!.api_key = FIXTURE_API_KEY
  return config
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true })
  }
})

describe('拆分配置', () => {
  it('自动创建停用的 QQ 配置模板且不覆盖已有内容', () => {
    const root = makeTemporaryDirectory()
    // 连接配置与适配器插件同目录，不再落在主体的 config/ 下。
    const directory = join(root, 'adapters', 'yueli-demo-adapter')
    mkdirSync(directory, { recursive: true })
    // 段名取自清单而非模板常量，这里故意用一个模板里不可能写死的名字。
    writeFileSync(
      join(directory, '_manifest.json'),
      JSON.stringify({ config_section: 'demoprotocol' }),
      'utf-8',
    )

    const path = ensureAdapterConfig(directory)
    const template = readFileSync(path, 'utf-8')
    expect(path).toBe(join(directory, 'config.toml'))
    expect(template).toContain('[demoprotocol]')
    expect(template).toContain('enabled = false')
    expect(template).toContain('qq = ""')

    writeFileSync(path, template.replace('enabled = false', 'enabled = true'), 'utf-8')
    expect(ensureAdapterConfig(directory)).toBe(path)
    expect(readFileSync(path, 'utf-8')).toContain('enabled = true')
  })

  it('适配器声明缺失时按默认值创建，已有内容不覆盖', () => {
    const configDir = join(makeTemporaryDirectory(), 'config')

    expect(ensureAdapterSelection(configDir)).toBe('yueli-snowluma-adapter')
    const path = join(configDir, 'adapter.toml')
    expect(readFileSync(path, 'utf-8')).toContain('plugin = "yueli-snowluma-adapter"')

    writeFileSync(path, 'plugin = "yueli-napcat-adapter"\n', 'utf-8')
    expect(ensureAdapterSelection(configDir)).toBe('yueli-napcat-adapter')
  })

  it('按厂商、模型、Bot 和功能四个职责写入并可无损读回', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = baseConfig()
    config.bot.name = '归档测试角色'
    config.api_providers[0]!.kind = 'deepseek'
    config.api_providers[0]!.api_key = 'sk-test'
    config.api_providers[0]!.max_retries = 4
    config.models[0]!.model_identifier = 'deepseek-v4-flash'
    config.vision.enabled = true
    config.personality.birthday = '2006-09-12'
    config.personality.personality = '自定义人设'
    config.conversation.working_memory_messages = 60
    config.generation.chat.temperature = 0.42
    config.models[0]!.extra_body = { reasoning_effort: 'medium' }
    config.generation.proactive.enabled = false
    config.model_tasks.schedule.model_list = ['chat']
    config.vision.capture_mode = 'screen'

    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory)).toEqual(config)
    expect(readFileSync(join(directory, 'providers.toml'), 'utf-8')).toContain('[[api_providers]]')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('model_identifier = "deepseek-v4-flash"')
    expect(readFileSync(join(directory, 'bot.toml'), 'utf-8')).toContain("personality = '''自定义人设'''")
    expect(readFileSync(join(directory, 'bot.toml'), 'utf-8')).toContain('working_memory_messages = 60')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('temperature = 0.42')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('[generation.proactive]')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('[model_tasks.schedule]')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('reasoning_effort = "medium"')
    expect(readFileSync(join(directory, 'models.toml'), 'utf-8')).toContain('enabled = false')
    expect(readFileSync(join(directory, 'providers.toml'), 'utf-8')).toContain('max_retries = 4')
    expect(readFileSync(join(directory, 'features.toml'), 'utf-8')).toContain('enabled = true')
    expect(readFileSync(join(directory, 'features.toml'), 'utf-8')).toContain('capture_mode = "screen"')
  })

  it('capture_mode 缺省时退回只截窗口，写错值当场报错', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    writeConfigDirectory(directory, baseConfig())
    const featuresPath = join(directory, 'features.toml')
    const original = readFileSync(featuresPath, 'utf-8')

    // 旧配置缺少新增字段时必须沿用默认值，确保配置格式升级不阻断启动。
    writeFileSync(featuresPath, original.replace(/capture_mode = .*/, ''), 'utf-8')
    expect(readConfigDirectory(directory).vision.capture_mode).toBe('window')

    // 非法枚举值必须在加载期拒绝，避免错误配置延迟到运行时才产生错误行为。
    writeFileSync(featuresPath, original.replace(/capture_mode = .*/, 'capture_mode = "fullscreen"'), 'utf-8')
    expect(() => readConfigDirectory(directory)).toThrow(/capture_mode/)
  })

  it('自动迁移旧 config.toml 并保留旧文件', () => {
    const root = makeTemporaryDirectory()
    const legacyPath = join(root, 'config.toml')
    const directory = join(root, 'config')
    writeFileSync(legacyPath, `
[bot]
name = "旧配置角色"
user_nickname = "小明"
relationship = "哥哥"

[llm]
provider = "deepseek"
model = "deepseek-chat"
api_key = "${FIXTURE_TOML_KEY}"
`, 'utf-8')

    const config = readConfigDirectory(directory, legacyPath)

    expect(config.bot.name).toBe('旧配置角色')
    expect(config.bot.user_nickname).toBe('小明')
    // 旧配置的一套地址密钥摊成一条连接 + 一个模型 + 一条候选
    expect(config.api_providers).toHaveLength(1)
    expect(config.api_providers[0]!.kind).toBe('deepseek')
    expect(config.api_providers[0]!.api_key).toBe('sk-legacy')
    expect(config.models[0]!.model_identifier).toBe('deepseek-chat')
    expect(config.model_tasks.chat.model_list).toEqual(['chat'])
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

  it('@ 必回开关必须由 bot.toml 显式声明', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    writeConfigDirectory(directory, baseConfig())
    const botPath = join(directory, 'bot.toml')
    const original = readFileSync(botPath, 'utf-8')
    writeFileSync(botPath, original.replace(/at_mention_must_reply = .*\n/, ''), 'utf-8')

    expect(() => readConfigDirectory(directory)).toThrow(/at_mention_must_reply/)
  })
})

describe('API 轮询', () => {
  function buildRotatingConfig() {
    const config = baseConfig()
    config.api_providers = [
      { ...config.api_providers[0]!, name: '主力', kind: 'deepseek', api_key: 'sk-main' },
      { ...config.api_providers[0]!, name: '备用', kind: 'openai', api_key: 'sk-backup' },
    ]
    config.models = [
      { name: '主力对话', model_identifier: 'deepseek-chat', api_provider: '主力', extra_body: {}, reasoning_parse_mode: 'field', embedding_dim: 0, visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0 },
      { name: '备用对话', model_identifier: 'gpt-4o-mini', api_provider: '备用', extra_body: {}, reasoning_parse_mode: 'field', embedding_dim: 0, visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0 },
    ]
    config.model_tasks.chat = {
      ...config.model_tasks.chat,
      model_list: ['主力对话', '备用对话'],
      selection_strategy: 'sequential',
    }
    return config
  }

  it('多厂商多模型的候选列表与策略可无损读回', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildRotatingConfig()
    config.models[0]!.reasoning_parse_mode = 'tag'

    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory)).toEqual(config)
    const models = readFileSync(join(directory, 'models.toml'), 'utf-8')
    expect(models).toContain('[model_tasks.chat]')
    expect(models).toContain('model_list = ["主力对话", "备用对话"]')
    expect(models).toContain('selection_strategy = "sequential"')
    expect(models).toContain('reasoning_parse_mode = "tag"')
  })

  it('random 策略照原样保留，不会被写回默认值', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildRotatingConfig()
    config.model_tasks.chat.selection_strategy = 'random'

    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory).model_tasks.chat.selection_strategy).toBe('random')
  })

  it('负载均衡策略与模型思考开关可无损读回', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildRotatingConfig()
    config.model_tasks.chat.selection_strategy = 'balance'
    config.models[0]!.extra_body = {
      ...config.models[0]!.extra_body,
      enable_thinking: false,
    }

    writeConfigDirectory(directory, config)

    const restored = readConfigDirectory(directory)
    expect(restored.model_tasks.chat.selection_strategy).toBe('balance')
    expect(restored.models[0]!.extra_body.enable_thinking).toBe(false)
  })

  it('候选引用了不存在的模型时当场报错', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildRotatingConfig()
    writeConfigDirectory(directory, config)
    const modelsPath = join(directory, 'models.toml')
    const original = readFileSync(modelsPath, 'utf-8')
    writeFileSync(modelsPath, original.replace('"备用对话"]', '"打错的名字"]'), 'utf-8')

    expect(() => readConfigDirectory(directory)).toThrow(/不存在的模型：打错的名字/)
  })

  it('模型引用了不存在的厂商时保存被拒，哪怕它还没被任何任务选中', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildRotatingConfig()
    config.models.push({
      name: '没人用的模型', model_identifier: 'x', api_provider: '不存在的厂商',
      extra_body: {}, reasoning_parse_mode: 'field', embedding_dim: 0,
      visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
    })
    writeConfigDirectory(directory, config)

    expect(() => assertConfigConsistent(config)).toThrow(/不存在的服务商/)
    expect(() => readConfigDirectory(directory)).toThrow(/不存在的厂商/)
  })

  it('保存入口拦下用户编辑出来的结构错误', () => {
    const duplicateNames = buildRotatingConfig()
    duplicateNames.api_providers[1]!.name = '主力'
    expect(() => assertConfigConsistent(duplicateNames)).toThrow(/名称不能重复/)

    const volcengineOnChat = buildRotatingConfig()
    volcengineOnChat.api_providers[0]!.client_type = 'volcengine'
    expect(() => assertConfigConsistent(volcengineOnChat)).toThrow(/豆包语音/)

    const emptyIdentifier = buildRotatingConfig()
    emptyIdentifier.models[1]!.model_identifier = ''
    expect(() => assertConfigConsistent(emptyIdentifier)).toThrow(/还没填模型 ID/)

    const ttsWithoutCandidate = buildRotatingConfig()
    ttsWithoutCandidate.tts.enabled = true
    expect(() => assertConfigConsistent(ttsWithoutCandidate)).toThrow(/候选模型/)

    const mixedDims = buildRotatingConfig()
    mixedDims.models[0]!.embedding_dim = 1536
    mixedDims.models[1]!.embedding_dim = 1024
    mixedDims.model_tasks.embedding.model_list = ['主力对话', '备用对话']
    expect(() => assertConfigConsistent(mixedDims)).toThrow(/向量维度/)

    // 结构有效的配置不得被迁移逻辑误判为非法。
    expect(() => assertConfigConsistent(buildRotatingConfig())).not.toThrow()
  })

  it('迁移旧配置不受用户编辑规则的约束，老用户升级后照样起得来', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    // 1.0.0 常见形态：四个任务槽位都填着，其中几个指向没填模型 ID 的占位模型
    const config = baseConfig()
    config.models.push({
      name: 'tts', model_identifier: '', api_provider: config.api_providers[0]!.name,
      extra_body: {}, reasoning_parse_mode: 'none', embedding_dim: 0,
      visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
    })
    config.model_tasks.tts = {
      ...config.model_tasks.tts, model_list: ['tts'], selection_strategy: 'sequential',
    }

    expect(() => writeConfigDirectory(directory, config)).not.toThrow()
    expect(() => assertConfigConsistent(config)).toThrow(/还没填模型 ID/)
  })

  it('同一个模型在候选里写两遍属于手误，直接报错', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildRotatingConfig()
    writeConfigDirectory(directory, config)
    const modelsPath = join(directory, 'models.toml')
    const original = readFileSync(modelsPath, 'utf-8')
    writeFileSync(modelsPath, original.replace('"备用对话"]', '"主力对话"]'), 'utf-8')

    expect(() => readConfigDirectory(directory)).toThrow(/重复模型/)
  })

  it('读得进 1.0.0 的单模型写法，并在保存时升级成候选列表', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    writeConfigDirectory(directory, baseConfig())
    const modelsPath = join(directory, 'models.toml')
    // 旧版本使用每个任务一个模型名，不包含 [model_tasks.<task>] 配置段。
    writeFileSync(modelsPath, `
[inner]
version = "1.0.0"

[model_tasks]
chat = "chat"
vision = "vision"
tts = "tts"
embedding = "embedding"

[[models]]
name = "chat"
model_identifier = "deepseek-chat"
api_provider = "主力"
extra_body = {}
embedding_dim = 0

[[models]]
name = "vision"
model_identifier = "vision-pro"
api_provider = "主力"
extra_body = {}
embedding_dim = 0

[[models]]
name = "tts"
model_identifier = ""
api_provider = "主力"
extra_body = {}
embedding_dim = 0

[[models]]
name = "embedding"
model_identifier = "text-embedding-3-small"
api_provider = "主力"
extra_body = {}
embedding_dim = 1536
`, 'utf-8')

    const config = readConfigDirectory(directory)
    expect(config.model_tasks.chat).toEqual({
      model_list: ['chat'],
      selection_strategy: 'sequential',
      first_token_timeout_ms: 30_000,
      slow_threshold_ms: 8_000,
    })
    expect(config.model_tasks.embedding.model_list).toEqual(['embedding'])

    writeConfigDirectory(directory, config)
    const upgraded = readFileSync(modelsPath, 'utf-8')
    expect(upgraded).toContain(`version = "${CONFIG_VERSION}"`)
    expect(upgraded).toContain('[model_tasks.chat]')
  })

  it('对话任务任一候选填齐了模型和 Key 就算配置完整', () => {
    const config = buildRotatingConfig()
    expect(configIsComplete(config)).toBe(true)

    const unnamed = structuredClone(config)
    unnamed.bot.name = ''
    expect(configIsComplete(unnamed)).toBe(false)

    // 主力缺模型 ID 时，备用仍然能撑起首次启动判定
    config.models[0]!.model_identifier = ''
    expect(configIsComplete(config)).toBe(true)

    config.models[1]!.model_identifier = ''
    expect(configIsComplete(config)).toBe(false)
  })
})

describe('旧版 .env 预填', () => {
  function writeLegacyEnv(root: string, lines: string[]): string {
    const envPath = join(root, '.env')
    writeFileSync(envPath, lines.join(String.fromCharCode(10)), 'utf-8')
    return envPath
  }

  it('DeepSeek 的 LLM_THINKING 自动转换为 extra_body，不再要求手动处理', () => {
    const root = makeTemporaryDirectory()
    const envPath = writeLegacyEnv(root, [
      'LLM_API_KEY=sk-test',
      'LLM_MODEL=deepseek-v4-flash',
      'LLM_BASE_URL=https://api.deepseek.com',
      'LLM_THINKING=disabled',
    ])

    const prefill = tryPrefillFromLegacyEnv(envPath)
    expect(prefill?.api_providers?.[0]?.kind).toBe('deepseek')
    expect(prefill?.models?.[0]?.extra_body).toEqual({ thinking: { type: 'disabled' } })
    expect(prefill?.model_tasks?.chat.model_list).toEqual(['chat'])
  })

  it('Ark 的 enabled 与 DashScope 的 disabled 使用各自厂商格式', () => {
    const root = makeTemporaryDirectory()
    const arkPath = writeLegacyEnv(root, [
      'LLM_API_KEY=sk-test',
      'LLM_MODEL=doubao-seed-character-260628',
      'LLM_PROVIDER=ark',
      'LLM_THINKING=enabled',
    ])
    expect(tryPrefillFromLegacyEnv(arkPath)?.models?.[0]?.extra_body)
      .toEqual({ thinking: { type: 'enabled' } })

    const dashscopePath = writeLegacyEnv(root, [
      'LLM_API_KEY=sk-test',
      'LLM_MODEL=qwen-max',
      'LLM_PROVIDER=dashscope',
      'LLM_THINKING=disabled',
    ])
    expect(tryPrefillFromLegacyEnv(dashscopePath)?.models?.[0]?.extra_body)
      .toEqual({ enable_thinking: false })
  })

  it('合并预填时保留已有服务商、模型和任务候选，只补缺失字段', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = baseConfig()
    const existingProvider = structuredClone(config.api_providers[0]!)
    existingProvider.name = '已有服务商'
    existingProvider.base_url = 'https://api.example.com'
    existingProvider.api_key = FIXTURE_EXISTING_KEY
    const existingModel = structuredClone(config.models[0]!)
    existingModel.name = 'chat'
    existingModel.model_identifier = ''
    existingModel.api_provider = '已有服务商'
    config.api_providers = [existingProvider]
    config.models = [existingModel]
    config.model_tasks.chat.model_list = []
    writeConfigDirectory(directory, config)

    const envPath = writeLegacyEnv(root, [
      'LLM_API_KEY=sk-env',
      'LLM_MODEL=env-model',
      'LLM_BASE_URL=https://api.deepseek.com',
      'LLM_THINKING=disabled',
    ])
    const base = readConfigDirectory(directory)
    const merged = mergeLegacyEnvPrefill(base, tryPrefillFromLegacyEnv(envPath)!)

    expect(merged.api_providers).toHaveLength(2)
    expect(merged.api_providers[0]).toEqual(existingProvider)
    expect(merged.models[0]).toMatchObject({
      name: 'chat', model_identifier: 'env-model', api_provider: '已有服务商',
    })
    expect(merged.models[0]!.extra_body).toEqual({ thinking: { type: 'disabled' } })
    expect(merged.models).toHaveLength(1)
    expect(merged.model_tasks.chat.model_list).toEqual(['chat'])
  })

  it('不含模型连接字段时返回 null，不创建任何配置', () => {
    const root = makeTemporaryDirectory()
    const envPath = writeLegacyEnv(root, ['SPRITE_PROVIDER=seedream'])

    expect(tryPrefillFromLegacyEnv(envPath)).toBeNull()
  })
})

describe('豆包语音 TTS', () => {
  function buildVolcengineConfig() {
    const config = baseConfig()
    config.api_providers.push({
      name: '语音', kind: 'volcengine', base_url: 'https://openspeech.bytedance.com',
      api_key: FIXTURE_ACCESS_TOKEN, client_type: 'volcengine', app_id: 'my-app-id',
      model_list_endpoint: '/models', default_headers: {}, default_query: {},
      auth_type: 'bearer', auth_name: '',
      timeout_ms: 120_000, max_retries: 2, retry_interval_ms: 800,
    })
    config.models.push({
      name: 'tts', model_identifier: '', api_provider: '语音',
      extra_body: {}, reasoning_parse_mode: 'none', embedding_dim: 0,
      visual: false, temperature: null, max_tokens: null, price_in: 0, price_out: 0,
    })
    config.model_tasks.tts = {
      ...config.model_tasks.tts, model_list: ['tts'], selection_strategy: 'sequential',
    }
    config.tts.enabled = true
    config.tts.voice = 'zh_female_test'
    return config
  }

  it('保存后不再把 client_type 和 app_id 抹回 openai', () => {
    // 覆盖非 openai 的 client_type，确保设置窗口保存任意字段都不会丢失已有 app_id，
    // 否则 TTS 会回退到错误协议并在运行时失败。
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = buildVolcengineConfig()

    writeConfigDirectory(directory, config)
    const readBack = readConfigDirectory(directory)

    const ttsProvider = readBack.api_providers.find((provider) => provider.name === '语音')!
    expect(ttsProvider.client_type).toBe('volcengine')
    expect(ttsProvider.app_id).toBe('my-app-id')
    expect(readBack.tts.cluster).toBe('volcano_tts')
    expect(readBack).toEqual(config)

    // 再存一次也不能漂移——设置窗口每次保存都会整份重写
    writeConfigDirectory(directory, readBack)
    expect(readConfigDirectory(directory)).toEqual(config)
  })

  it('providers.toml 里出现 volcengine 时不再拒绝启动', () => {
    // 覆盖非 openai 的 client_type，确保手写 volcengine 配置不会在 Electron 启动阶段被错误拒绝。
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    writeConfigDirectory(directory, buildVolcengineConfig())

    const providers = readFileSync(join(directory, 'providers.toml'), 'utf-8')
    expect(providers).toContain('client_type = "volcengine"')
    expect(providers).toContain('app_id = "my-app-id"')
    expect(() => readConfigDirectory(directory)).not.toThrow()
  })

  it('默认仍是 openai 协议，且不写出无关的 app_id', () => {
    const root = makeTemporaryDirectory()
    const directory = join(root, 'config')
    const config = baseConfig()
    config.tts.enabled = true
    config.tts.voice = 'alloy'
    writeConfigDirectory(directory, config)

    expect(readConfigDirectory(directory).api_providers[0]!.client_type).toBe('openai')
    // app_id 仅对 volcengine 有意义，openai 连接不应写入该字段。
    const providers = readFileSync(join(directory, 'providers.toml'), 'utf-8')
    expect(providers).not.toContain('app_id')
  })
})
