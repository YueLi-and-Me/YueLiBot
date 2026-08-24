/**
 * 把 Electron 写入器的默认配置写到指定目录，供配置对齐校验（scripts/config_parity.py）
 * 与升级演练使用。产物就是 writeConfigDirectory 会整份重写出的那份文件。
 *
 *   npx tsx scripts/config_defaults.ts <目标目录>
 */
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

import { cloneDefaults, writeConfigDirectory } from '../electron/main/config.ts'

const target = process.argv[2]
if (!target) {
  console.error('用法：npx tsx scripts/config_defaults.ts <目标目录>')
  process.exit(2)
}

const directory = resolve(target)
mkdirSync(directory, { recursive: true })
writeConfigDirectory(directory, cloneDefaults())
console.log(`默认配置已写入 ${directory}`)
