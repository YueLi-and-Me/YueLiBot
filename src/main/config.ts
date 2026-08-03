import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { parse as parseDotenv } from 'dotenv'
import * as TOML from 'smol-toml'
import type { YueliConfig } from '../shared/ipc.ts'

/**
 * config.toml 的读写。字段名和 Python 侧的 pydantic 模型一一对应
 * （见 python/yueli/config/schema.py），改一边记得改另一边。
 */

export const DEFAULT_CONFIG: YueliConfig = {
  bot: { user_nickname: '', relationship: '' },
  llm: { provider: 'ark', model: '', base_url: '', api_key: '', thinking: 'disabled', timeout_ms: 120_000 },
  tts: { enabled: false, base_url: '', api_key: '', model: '', voice: '', format: 'mp3', speed: 0.95 },
  vision: {
    enabled: false, model: '', api_key: '', base_url: '',
    folder_enabled: false, fullscreen_silent: true,
  },
  vector: {
    enabled: false, embedding_base_url: '', embedding_api_key: '',
    embedding_model: 'text-embedding-3-small', embedding_dim: 1536,
  },
  advanced: { log_level: 'INFO', https_proxy: '' },
}

export function readConfigFile(path: string): YueliConfig {
  if (!existsSync(path)) return structuredClone(DEFAULT_CONFIG)
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const parsed = TOML.parse(readFileSync(path, 'utf-8')) as any
    // 浅合并每个 section：用户手改文件删掉某一行也不该让整个配置读取失败，
    // 缺的字段就用默认值兜底。
    return {
      bot: { ...DEFAULT_CONFIG.bot, ...parsed.bot },
      llm: { ...DEFAULT_CONFIG.llm, ...parsed.llm },
      tts: { ...DEFAULT_CONFIG.tts, ...parsed.tts },
      vision: { ...DEFAULT_CONFIG.vision, ...parsed.vision },
      vector: { ...DEFAULT_CONFIG.vector, ...parsed.vector },
      advanced: { ...DEFAULT_CONFIG.advanced, ...parsed.advanced },
    }
  } catch (err) {
    console.warn('[config] 读取 config.toml 失败，用默认值：', err)
    return structuredClone(DEFAULT_CONFIG)
  }
}

/** 首次启动判定：模型和 Key 都填了才算配置完整，其它一切都有默认值兜底。 */
export function configIsComplete(cfg: YueliConfig): boolean {
  return cfg.llm.model.trim() !== '' && cfg.llm.api_key.trim() !== ''
}

function tomlString(v: string): string {
  const escaped = v
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r')
    .replace(/\t/g, '\\t')
  return `"${escaped}"`
}

function tomlValue(v: string | number | boolean): string {
  if (typeof v === 'boolean' || typeof v === 'number') return String(v)
  return tomlString(v)
}

/**
 * 手写模板，不用通用 TOML 序列化库——通用 stringify 不带注释，每次保存都会把
 * 注释丢光。这里每个字段的注释固定写在模板里，保存时整份重新生成、注释
 * 永远齐全；代价是用户如果手改 TOML 加自己的注释，会在下次从设置窗口保存时
 * 被覆盖掉。对这个项目的量级（一个人用、字段个位数），这个取舍划算。
 */
export function serializeConfig(cfg: YueliConfig): string {
  return `# YueLiBot 配置文件——由设置窗口生成。可以手改，但下次在设置窗口里保存
# 会整份重写（含这份注释），手加的注释不会保留。

[bot]
# 你希望她怎么称呼你，留空则不特别用名字称呼你
user_nickname = ${tomlValue(cfg.bot.user_nickname)}
# 她和你的关系，例如"哥哥"、"姐姐"、"朋友"；留空则不设定这层关系
relationship = ${tomlValue(cfg.bot.relationship)}

[llm]
# 预设：ark | deepseek | dashscope | moonshot | openai | ollama
provider = ${tomlValue(cfg.llm.provider)}
# 模型 ID。方舟必填（版本号频繁变动），到 https://console.volcengine.com/ark 复制
model = ${tomlValue(cfg.llm.model)}
api_key = ${tomlValue(cfg.llm.api_key)}
# 留空则用预设的官方地址
base_url = ${tomlValue(cfg.llm.base_url)}
# 深度思考：disabled（默认，推荐）| enabled | auto
# 开启后实测首字延迟从 3 秒涨到 26~31 秒，桌宠场景里这等于产品报废
thinking = ${tomlValue(cfg.llm.thinking)}
timeout_ms = ${tomlValue(cfg.llm.timeout_ms)}

[tts]
# 不开就是纯文字，其余功能不受影响
enabled = ${tomlValue(cfg.tts.enabled)}
base_url = ${tomlValue(cfg.tts.base_url)}
api_key = ${tomlValue(cfg.tts.api_key)}
model = ${tomlValue(cfg.tts.model)}
voice = ${tomlValue(cfg.tts.voice)}
format = ${tomlValue(cfg.tts.format)}
# 陪伴场景略慢一点更自然，太快像播报
speed = ${tomlValue(cfg.tts.speed)}

[vision]
# 全项目唯一会把屏幕内容送上云的功能，开启前想清楚：
# 只截前台那一个窗口、绝不落盘、缩到 768px 宽再传，但截图里仍可能有
# 明文密码、私信、银行页面、公司文档
enabled = ${tomlValue(cfg.vision.enabled)}
# 留空则复用对话配置，仅当对话接口本身接受 image_url 时可用
base_url = ${tomlValue(cfg.vision.base_url)}
model = ${tomlValue(cfg.vision.model)}
api_key = ${tomlValue(cfg.vision.api_key)}
# 资源管理器截图常带路径、文档名和下载记录，隐私风险更高，默认关
folder_enabled = ${tomlValue(cfg.vision.folder_enabled)}
# 疑似全屏时静默，避免直播/录屏把桌宠声音带进去
fullscreen_silent = ${tomlValue(cfg.vision.fullscreen_silent)}

[vector]
# 向量混合召回，默认关；还需 pip install yueli[vector]
enabled = ${tomlValue(cfg.vector.enabled)}
# 留空则复用 llm.base_url / llm.api_key
embedding_base_url = ${tomlValue(cfg.vector.embedding_base_url)}
embedding_api_key = ${tomlValue(cfg.vector.embedding_api_key)}
embedding_model = ${tomlValue(cfg.vector.embedding_model)}
embedding_dim = ${tomlValue(cfg.vector.embedding_dim)}

[advanced]
log_level = ${tomlValue(cfg.advanced.log_level)}
# 全局 HTTP(S) 代理，例如 http://127.0.0.1:7890
https_proxy = ${tomlValue(cfg.advanced.https_proxy)}
`
}

export function writeConfigFile(path: string, cfg: YueliConfig): void {
  mkdirSync(dirname(path), { recursive: true })
  writeFileSync(path, serializeConfig(cfg), 'utf-8')
}

/**
 * 老用户从仓库根目录的 .env 迁移：只在 config.toml 不存在时调用，
 * 读到多少填多少，体谅之前配过的人不用重填一遍。
 */
export function tryPrefillFromLegacyEnv(envPath: string): Partial<YueliConfig> | null {
  if (!existsSync(envPath)) return null
  try {
    const parsed = parseDotenv(readFileSync(envPath))
    if (!parsed.LLM_API_KEY && !parsed.LLM_MODEL) return null
    return {
      llm: {
        provider: parsed.LLM_PROVIDER || DEFAULT_CONFIG.llm.provider,
        model: parsed.LLM_MODEL || '',
        base_url: parsed.LLM_BASE_URL || '',
        api_key: parsed.LLM_API_KEY || '',
        thinking: (parsed.LLM_THINKING as YueliConfig['llm']['thinking']) || 'disabled',
        timeout_ms: Number(parsed.LLM_TIMEOUT_MS) || DEFAULT_CONFIG.llm.timeout_ms,
      },
    }
  } catch {
    return null
  }
}
