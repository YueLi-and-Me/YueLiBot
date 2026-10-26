/**
 * 用 Electron 写入器写出、读回 models.toml，供 Python 侧用例对照两个写入器的输出
 * （pytests/core/test_model_price_writers.py）。
 *
 *   npx tsx scripts/check/config_models_io.ts write <目标目录> <模型 JSON 文件>
 *       在默认配置后追加 JSON 里的模型条目，整份写出配置目录；默认厂商的空密钥
 *       补成占位值，写出的目录因此能被读取器完整读回。
 *   npx tsx scripts/check/config_models_io.ts read <配置目录>
 *       用 Electron 读取器读回配置目录，把模型列表以 JSON 打印到标准输出。
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { cloneDefaults, readConfigDirectory, writeConfigDirectory } from '../../electron/main/config.ts'
import type { ModelDefinitionConfig } from '../../electron/shared/ipc.ts'

const [mode, target, modelsFile] = process.argv.slice(2)
if (mode === 'write' && target && modelsFile) {
  const directory = resolve(target)
  mkdirSync(directory, { recursive: true })
  const config = cloneDefaults()
  for (const provider of config.api_providers) {
    if (provider.auth_type !== 'none' && !provider.api_key) provider.api_key = 'fixture-key'
  }
  const models = JSON.parse(readFileSync(modelsFile, 'utf-8')) as ModelDefinitionConfig[]
  config.models.push(...models)
  writeConfigDirectory(directory, config)
} else if (mode === 'read' && target) {
  process.stdout.write(JSON.stringify(readConfigDirectory(resolve(target)).models))
} else {
  console.error('用法：npx tsx scripts/check/config_models_io.ts write <目录> <模型 JSON> | read <目录>')
  process.exit(2)
}
